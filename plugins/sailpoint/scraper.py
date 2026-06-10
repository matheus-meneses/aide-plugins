import os
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

from playwright.sync_api import BrowserContext, Page, Playwright, Response, sync_playwright

from aide_sdk.base import BaseScraper
from aide_sdk.models import PluginEntry

LOGIN_INDICATORS = ["login.microsoftonline.com", "identitynow.com/login", "/oauth"]


def _sessions_dir() -> Path:
    aide_home = os.environ.get("AIDE_HOME") or str(Path.home() / ".aide")
    p = Path(aide_home) / "plugins" / "sailpoint" / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


class SailpointScraper(BaseScraper):
    name = "sailpoint"
    version = "1.0.0"
    categories: ClassVar[list[str]] = ["approval", "task"]

    def validate_config(self, config: dict) -> None:
        if "base_url" not in config:
            raise ValueError("Missing 'base_url' in config")

    def scrape(self, config: dict, secrets: dict) -> list[PluginEntry]:
        sessions = _sessions_dir()
        self._session_file = sessions / "sailpoint.json"
        base_url = config["base_url"]

        portal_domain = base_url.split("//")[-1].split("/")[0]
        self._portal_domain = portal_domain
        self._api_domain = portal_domain.replace(".identitynow.com", ".api.identitynow.com")

        self._log("Starting browser...")
        with sync_playwright() as p:
            context = self._get_context(p)
            page = context.new_page()
            self._log("Navigating to portal...")
            page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            if self._needs_login(page):
                self._log("Session expired. Attempting headless auto-auth...")
                self._try_auto_select_account(page)
                try:
                    page.wait_for_url(f"**/{self._portal_domain}/**", timeout=30000)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass

                if self._needs_login(page):
                    self._log("Headless auth failed. Opening visible browser...")
                    context.close()
                    context = self._manual_login(p, base_url)
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    self._log("Headless auto-auth successful!")
            page.close()

            self._log("Authenticated. Extracting data...")
            self._save_session(context)

            entries: list[PluginEntry] = []
            approvals = self._extract_pending_approvals(context)
            entries.extend(approvals)
            certs = self._extract_certifications(context)
            entries.extend(certs)

            self._log(
                f"Done. {len(entries)} entries collected ({len(approvals)} approvals, {len(certs)} certifications)."
            )
            context.close()
            return entries

    def _log(self, msg: str) -> None:
        self.log.debug(msg)

    def _get_context(self, p: Playwright) -> BrowserContext:
        browser = p.chromium.launch(headless=True)
        if self._session_file.exists():
            context = browser.new_context(storage_state=str(self._session_file), ignore_https_errors=True)
        else:
            context = browser.new_context(ignore_https_errors=True)
        return context

    def _needs_login(self, page: Page) -> bool:
        url = page.url
        return any(indicator in url for indicator in LOGIN_INDICATORS) or self._portal_domain not in url

    def _manual_login(self, p: Playwright, base_url: str) -> BrowserContext:
        self._log("Opening browser for authentication (auto-SSO or manual)...")
        browser = p.chromium.launch(headless=False)
        if self._session_file.exists():
            context = browser.new_context(storage_state=str(self._session_file), ignore_https_errors=True)
        else:
            context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        if self._needs_login(page):
            self._try_auto_select_account(page)
            page.wait_for_url(f"**/{self._portal_domain}/**", timeout=300000)
            page.wait_for_timeout(5000)

        self._log("Login successful!")
        self._save_session(context)
        return context

    def _try_auto_select_account(self, page: Page) -> None:
        try:
            account_tile = page.locator(
                '[data-test-id="list-item"], .table[role="presentation"] td, [id*="tilesHolder"] > div'
            ).first
            if account_tile.is_visible(timeout=3000):
                self._log("  Auto-selecting account in SSO picker...")
                account_tile.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

    def _save_session(self, context: BrowserContext) -> None:
        context.storage_state(path=str(self._session_file))

    def _extract_pending_approvals(self, context: BrowserContext) -> list[PluginEntry]:
        page = context.new_page()
        approvals_url = f"https://{self._portal_domain}/ui/d/approvals"
        approvals_endpoint = "access-request-approvals/pending"

        captured_approvals: list[dict] = []
        total_count = 0

        def on_response(response: Response) -> None:
            nonlocal total_count
            if approvals_endpoint in response.url and response.status == 200:
                try:
                    total_count = int(response.headers.get("x-total-count", "0"))
                    captured_approvals.extend(response.json())
                except Exception:
                    pass

        page.on("response", on_response)
        self._log("Fetching pending approvals...")
        page.goto(approvals_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)
        self._log(f"  Approvals API returned {len(captured_approvals)} of {total_count}.")

        if total_count > len(captured_approvals):
            self._log("  Paginating remaining approvals...")
            offset = len(captured_approvals)
            while offset < total_count:
                url = (
                    f"https://{self._api_domain}/beta/{approvals_endpoint}"
                    f"?limit=50&offset={offset}&sorters=-created&owner-id=me&count=true"
                )
                page.evaluate(
                    """async (url) => {
                        const resp = await fetch(url);
                        const data = await resp.json();
                        window.__extraApprovals = (window.__extraApprovals || []).concat(data);
                    }""",
                    url,
                )
                page.wait_for_timeout(3000)
                extra = page.evaluate("window.__extraApprovals || []")
                captured_approvals.extend(extra)
                page.evaluate("window.__extraApprovals = []")
                offset += 50

        page.close()

        entries = []
        for item in captured_approvals:
            entry = self._parse_approval(item)
            if entry:
                entries.append(entry)
        return entries

    def _parse_approval(self, item: dict) -> PluginEntry | None:
        try:
            requested_object = item.get("requestedObject", {})
            requester = item.get("requester", {})
            requested_for = item.get("requestedFor", {})
            request_type = item.get("requestType", "GRANT_ACCESS")
            client_meta = item.get("clientMetadata", {})
            comment_obj = item.get("requesterComment", {})

            action = "Grant" if request_type == "GRANT_ACCESS" else request_type.replace("_", " ").title()
            access_name = requested_object.get("name", "Unknown")
            title = f"{action}: {access_name}"

            detail_parts = []
            if comment_obj and comment_obj.get("comment"):
                detail_parts.append(comment_obj["comment"])

            forward_history = item.get("forwardHistory", [])
            if forward_history:
                chain = " → ".join(h.get("newApproverName", "?") for h in forward_history)
                detail_parts.append(f"Reassigned: {chain}")

            request_created = item.get("requestCreated", "")
            approval_id = item.get("id", "")
            web_url = (
                f"https://{self._portal_domain}/ui/d/approvals/{approval_id}"
                if approval_id
                else f"https://{self._portal_domain}/ui/d/approvals"
            )

            return PluginEntry(
                source="sailpoint",
                member=requested_for.get("name", "Unknown"),
                category="approval",
                title=title,
                detail="\n".join(detail_parts) if detail_parts else None,
                entry_date=self._parse_date(request_created),
                priority="warning",
                metadata={
                    "approval_id": approval_id,
                    "access_request_id": item.get("accessRequestId"),
                    "requester": requester.get("name"),
                    "requested_for": requested_for.get("name"),
                    "access_type": requested_object.get("type"),
                    "app_name": client_meta.get("requestedAppName", ""),
                    "request_type": request_type,
                    "created": item.get("created"),
                    "web_url": web_url,
                },
            )
        except Exception as e:
            self.log.warning(f"failed to parse approval item: {e}")
            return None

    def _extract_certifications(self, context: BrowserContext) -> list[PluginEntry]:
        page = context.new_page()
        certifications_url = f"https://{self._portal_domain}/ui/d/certifications"

        captured_certs: list[dict] = []

        def on_response(response: Response) -> None:
            url = response.url
            if response.status != 200:
                return
            if "certification" in url and self._api_domain in url:
                try:
                    body = response.json()
                    if isinstance(body, list):
                        captured_certs.extend(body)
                    elif isinstance(body, dict) and "items" in body:
                        captured_certs.extend(body["items"])
                except Exception:
                    pass

        page.on("response", on_response)
        self._log("Fetching certifications...")
        page.goto(certifications_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)

        if not captured_certs:
            self._log("  No certs from page load, querying API directly...")
            result = page.evaluate(f"""async () => {{
                const endpoints = [
                    '/beta/certifications?limit=250&filters=reviewer eq "me" and phase eq "ACTIVE"',
                    '/beta/certifications?limit=250&filters=phase eq "ACTIVE"',
                    '/v3/certifications?limit=250',
                    '/cc/api/certification/list'
                ];
                for (const ep of endpoints) {{
                    try {{
                        const url = 'https://{self._api_domain}' + ep;
                        const resp = await fetch(url);
                        if (resp.ok) {{
                            const data = await resp.json();
                            const items = Array.isArray(data) ? data : (data.items || data.objects || []);
                            if (items.length > 0) return {{endpoint: ep, items: items, count: items.length}};
                        }}
                    }} catch(e) {{}}
                }}
                return {{endpoint: null, items: [], count: 0}};
            }}""")
            if result and result.get("items"):
                self._log(f"  Found {result['count']} certs via {result['endpoint']}")
                captured_certs.extend(result["items"])

        self._log(f"  Certifications found: {len(captured_certs)}.")
        page.close()

        entries = []
        for item in captured_certs:
            entry = self._parse_certification(item)
            if entry:
                entries.append(entry)
        return entries

    def _parse_certification(self, item: dict) -> PluginEntry | None:
        try:
            name = item.get("name") or item.get("campaignName") or "Certification"
            campaign_type = item.get("type") or item.get("campaignType") or ""
            phase = item.get("phase") or item.get("status") or ""
            due = item.get("due") or item.get("deadline") or item.get("endDate") or ""
            created = item.get("created") or item.get("startDate") or ""
            cert_id = item.get("id") or ""
            completed = item.get("completedEntities") or item.get("completed") or 0
            total = item.get("totalEntities") or item.get("total") or 0

            entry_dt = self._parse_date(due) if due else self._parse_date(created)

            detail_parts = []
            if campaign_type:
                detail_parts.append(f"Type: {campaign_type}")
            if phase:
                detail_parts.append(f"Phase: {phase}")
            if total:
                detail_parts.append(f"Progress: {completed}/{total}")
            if due:
                detail_parts.append(f"Due: {due[:10]}")

            priority = "warning"
            if due:
                try:
                    days_left = (self._parse_date(due) - date.today()).days
                    if days_left <= 3:
                        priority = "critical"
                except Exception:
                    pass

            return PluginEntry(
                source="sailpoint",
                member="me",
                category="task",
                title=f"Certification: {name}",
                detail=" | ".join(detail_parts) if detail_parts else None,
                entry_date=entry_dt,
                priority=priority,
                metadata={
                    "certification_id": cert_id,
                    "campaign_type": campaign_type,
                    "phase": phase,
                    "due": due[:10] if due else None,
                    "completed": completed,
                    "total": total,
                },
            )
        except Exception as e:
            self.log.warning(f"failed to parse certification item: {e}")
            return None

    def _parse_date(self, iso_str: str) -> date:
        try:
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            return date.today()
