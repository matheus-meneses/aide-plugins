import contextlib
import os
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from aide_sdk.base import BaseScraper
from aide_sdk.models import PluginEntry, TeamMemberEntry

BASE_DOMAIN = "portalrh.bancointer.com.br"
LOGIN_HASH = "#/login"
VACATIONS_PATH = "#/request/notifications/vacation"
ABSENCE_PATH = "#/absence"
TEAM_PATH = "#/teamManagement/overview"
BASE_URL_PREFIX = "https://portalrh.bancointer.com.br/FrameHTML/web/app/RH/PortalMeuRH/"


def _sessions_dir() -> Path:
    aide_home = os.environ.get("AIDE_HOME") or str(Path.home() / ".aide")
    p = Path(aide_home) / "plugins" / "rh_portal" / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


class RHPortalScraper(BaseScraper):
    name = "rh_portal"
    version = "1.0.0"
    categories: ClassVar[list[str]] = ["approval", "absence"]

    def validate_config(self, config: dict) -> None:
        if "base_url" not in config:
            raise ValueError("Missing 'base_url' in config")

    def scrape(self, config: dict, secrets: dict) -> list[PluginEntry]:
        self._session_file = _sessions_dir() / "rh_portal.json"
        base_url = config["base_url"]
        username = secrets.get("username", "")
        password = secrets.get("password", "")

        self._log("Starting browser...")
        with sync_playwright() as p:
            context = self._get_context(p)
            page = context.new_page()

            self._log("Navigating to portal...")
            page.goto(base_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            if self._needs_login(page):
                if not username or not password:
                    context.close()
                    raise ValueError("Session expired and no credentials available")
                self._log("Session expired. Logging in with AD credentials...")
                self._do_login(page, username, password)

            if self._needs_login(page):
                context.close()
                raise ValueError("Login failed - still on login page after authentication attempt")

            self._log("Authenticated. Saving session...")
            self._save_session(context)

            entries = []

            self._log("Extracting vacation approvals...")
            vacation_entries = self._extract_vacations(page, config)
            entries.extend(vacation_entries)
            self._log(f"  Found {len(vacation_entries)} vacation approvals")

            self._log("Extracting absences...")
            absence_entries = self._extract_absences(page, config)
            entries.extend(absence_entries)
            self._log(f"  Found {len(absence_entries)} absences")

            self._log(f"Done. {len(entries)} entries collected.")
            context.close()
            return entries

    def scrape_team(self, config: dict, secrets: dict) -> list[TeamMemberEntry]:
        self._session_file = _sessions_dir() / "rh_portal.json"
        if not self._session_file.exists():
            self._log("No session file found for team scraping — run scrape first to authenticate")
            return []

        max_depth = int(config.get("team_depth", 5))
        self._log(f"Scraping team tree (depth={max_depth})...")

        with sync_playwright() as p:
            context = self._get_context(p)
            page = context.new_page()

            captured_url: list[str] = []

            def on_response(response):
                if response.status == 200 and "/team/employees/" in response.url:
                    captured_url.append(response.url)

            page.on("response", on_response)
            page.goto(f"{BASE_URL_PREFIX}{TEAM_PATH}", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)

            members: list[TeamMemberEntry] = []
            seen_regs: set[str] = set()

            if captured_url:
                api_root = captured_url[0].split("/team/employees/")[0]
                self._log("  Walking team hierarchy via API...")
                members = self._walk_team(page, api_root, "%7Bcurrent%7D", "", 0, max_depth, seen_regs)
            else:
                self._log("  No API responses captured, falling back to DOM scraping")
                members = self._scrape_team_dom(page, seen_regs, max_depth)

            if not members:
                self._log("  No team members found")
            else:
                self._log(f"  Found {len(members)} team members")

            context.close()
            return members

    def _walk_team(
        self, page: Page, api_root: str, encoded_id: str, manager_reg: str, depth: int, max_depth: int, seen: set[str]
    ) -> list[TeamMemberEntry]:
        members: list[TeamMemberEntry] = []
        items = self._fetch_reports(page, api_root, encoded_id)
        indent = "    " * (depth + 1)

        for item in items:
            reg = str(item.get("registry") or "")
            if not reg or reg in seen:
                continue
            seen.add(reg)

            member = self._build_member(item, manager_reg)
            members.append(member)
            self._log(f"{indent}{member.name} ({member.role}) -> mgr={manager_reg or 'root'}")

            if depth + 1 < max_depth:
                child_id = str(item.get("id") or "").replace("|", "%7C")
                if child_id:
                    members.extend(self._walk_team(page, api_root, child_id, reg, depth + 1, max_depth, seen))

        return members

    def _fetch_reports(self, page: Page, api_root: str, encoded_id: str) -> list[dict]:
        items: list[dict] = []
        current_page = 1
        while True:
            url = (
                f"{api_root}/team/employees/{encoded_id}"
                f"?page={current_page}&pageSize=200&hierarchicalLevel=1"
                f"&attentionPoints=false&encodeEmployeeId=true&isExcel=false&isGetHiredNextMonth=true"
            )
            try:
                resp = page.evaluate(
                    """(u) => fetch(u, {credentials: 'include'}).then(r => r.ok ? r.json() : null)""",
                    url,
                )
            except Exception as e:
                self.log.warning(f"error fetching reports for {encoded_id}: {e}")
                break

            if not isinstance(resp, dict):
                break

            items.extend(resp.get("items", []) or [])
            if not resp.get("hasNext", False):
                break
            current_page += 1

        return items

    def _build_member(self, item: dict, manager_reg: str) -> TeamMemberEntry:
        contacts = item.get("contacts") or {}
        emails = contacts.get("emails") or []
        email = next(
            (e.get("email", "") for e in emails if e.get("email") and "inter.co" in e.get("email", "")),
            next((e.get("email", "") for e in emails if e.get("email")), ""),
        )
        return TeamMemberEntry(
            name=item.get("name") or "",
            email=email,
            role=item.get("roleDescription") or "",
            department=item.get("department") or "",
            branch=item.get("branchName") or "",
            registration=str(item.get("registry") or ""),
            manager_registration=manager_reg,
        )

    def _scrape_team_dom(self, page: Page, seen: set[str], max_depth: int) -> list[TeamMemberEntry]:
        members: list[TeamMemberEntry] = []
        queue: list[tuple[Page, int]] = [(page, 0)]

        while queue:
            current_page, depth = queue.pop(0)
            if depth >= max_depth:
                continue

            cards = current_page.locator(
                "po-list-view-content-template, .po-list-view-item, po-tree-view-item, .member-card, [class*='employee'], [class*='collaborator']"
            ).all()
            self._log(f"  Depth {depth}: {len(cards)} cards found")

            for card in cards:
                try:
                    text = card.inner_text().strip()
                    if not text:
                        continue

                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    name = ""
                    reg = ""
                    role = ""

                    for line in lines:
                        if not name and len(line) > 3 and line.replace(" ", "").isalpha():
                            name = line
                        elif not reg and line.isdigit() and 4 <= len(line) <= 8:
                            reg = line
                        elif not role and name and len(line) > 3 and line == line.upper():
                            role = line

                    if not name or reg in seen:
                        continue
                    seen.add(reg)
                    members.append(TeamMemberEntry(name=name, registration=reg, role=role))

                    if depth + 1 < max_depth:
                        try:
                            btn = card.locator(
                                "button[aria-label*='equipe'], button[aria-label*='team'], button[aria-label*='subordinat'], a[href*='teamManagement']"
                            ).first
                            if btn.is_visible(timeout=500):
                                btn.click()
                                current_page.wait_for_timeout(3000)
                                queue.append((current_page, depth + 1))
                                current_page.go_back()
                                current_page.wait_for_timeout(2000)
                        except Exception:
                            pass
                except Exception as e:
                    self.log.warning(f"error parsing card at depth {depth}: {e}")

        return members

    def _log(self, msg: str) -> None:
        self.log.debug(msg)

    def _get_context(self, p: Playwright) -> BrowserContext:
        browser = p.chromium.launch(headless=True)
        if self._session_file.exists():
            context = browser.new_context(storage_state=str(self._session_file))
        else:
            context = browser.new_context()
        return context

    def _needs_login(self, page: Page) -> bool:
        return LOGIN_HASH in page.url or BASE_DOMAIN not in page.url

    def _do_login(self, page: Page, username: str, password: str) -> None:
        page.wait_for_timeout(2000)

        username_field = page.locator('input[name="user"]')
        password_field = page.locator('input[name="password"]')

        username_field.wait_for(state="visible", timeout=15000)
        username_field.fill(username)

        password_field.wait_for(state="visible", timeout=5000)
        password_field.fill(password)

        page.locator('button.po-button:has-text("Enter")').click()
        page.wait_for_timeout(5000)

        with contextlib.suppress(Exception):
            page.wait_for_function(
                "!window.location.hash.includes('/login')",
                timeout=15000,
            )

    def _save_session(self, context: BrowserContext) -> None:
        context.storage_state(path=str(self._session_file))

    def _extract_vacations(self, page: Page, config: dict) -> list[PluginEntry]:
        page.goto(f"{BASE_URL_PREFIX}{VACATIONS_PATH}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

        entries = []
        rows = page.locator("tr.po-table-row").all()

        if not rows:
            self._log("  No vacation rows found in table")
            return entries

        for row in rows:
            try:
                cells = row.locator("td.po-table-column").all()
                if len(cells) < 5:
                    continue

                cell_texts = [c.inner_text().strip() for c in cells]
                name = cell_texts[0]
                vac_type = cell_texts[1] if len(cell_texts) > 1 else ""
                vesting = cell_texts[2] if len(cell_texts) > 2 else ""
                start = cell_texts[3] if len(cell_texts) > 3 else ""
                end = cell_texts[4] if len(cell_texts) > 4 else ""
                days = cell_texts[5] if len(cell_texts) > 5 else ""

                if not name:
                    continue

                title = f"{name} - {vac_type} ({start} to {end}, {days} days)"
                entry_date = self._parse_date_str(start) or date.today()

                entries.append(
                    PluginEntry(
                        source="rh_portal",
                        member=name,
                        category="approval",
                        title=title,
                        detail=f"Vesting: {vesting}",
                        entry_date=entry_date,
                        priority="warning",
                        metadata={
                            "name": name,
                            "type": vac_type,
                            "vesting": vesting,
                            "start": start,
                            "end": end,
                            "days": days,
                        },
                    )
                )
            except Exception as e:
                self.log.warning(f"error parsing vacation row: {e}")

        return entries

    def _extract_absences(self, page: Page, config: dict) -> list[PluginEntry]:
        page.goto(f"{BASE_URL_PREFIX}{ABSENCE_PATH}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)

        entries = []
        cards = page.locator("div.timeline-block").all()
        if not cards:
            self._log("  No absence timeline cards found")
            return entries

        for card in cards:
            try:
                text = card.inner_text().strip()
                if not text or "Vacation balance" not in text:
                    continue

                lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

                name = role = status_tag = balance = grant_period = ref_period = stage = ""

                i = 0
                while i < len(lines):
                    line = lines[i]
                    if i <= 2 and line.isdigit():
                        i += 1
                        continue
                    if i <= 3 and len(line) <= 3 and line.isalpha():
                        i += 1
                        continue

                    if not name and line[0].isalpha() and line == line.title():
                        name = line
                        i += 1
                        continue
                    if not role and name and line == line.upper() and len(line) > 3:
                        role = line
                        i += 1
                        if (
                            i < len(lines)
                            and lines[i] == lines[i].upper()
                            and len(lines[i]) > 3
                            and "BALANCE" not in lines[i].upper()
                        ):
                            status_tag = lines[i]
                            i += 1
                        continue
                    if "Vacation balance" in line or "balance" in line.lower():
                        i += 1
                        if i < len(lines):
                            balance = lines[i]
                            i += 1
                        continue
                    if "Grant vacation" in line or "grant" in line.lower():
                        i += 1
                        if i < len(lines):
                            grant_period = lines[i]
                            i += 1
                        continue
                    if "Period referring" in line or "referring" in line.lower():
                        i += 1
                        if i < len(lines):
                            ref_period = lines[i]
                            i += 1
                        continue
                    if "Stage" in line or "stage" in line.lower():
                        i += 1
                        if i < len(lines):
                            stage = lines[i]
                            i += 1
                        continue
                    i += 1

                if not name:
                    continue

                priority = "info"
                if status_tag in ("EXPIRED", "DOUBLE RISK", "TO EXPIRE"):
                    priority = "warning"

                grant_end = ""
                if "until" in grant_period:
                    parts = grant_period.split("until")
                    grant_end = parts[-1].strip() if len(parts) > 1 else ""

                entry_date = self._parse_date_str(grant_end) or date.today()
                title = f"{name} - {balance} ({status_tag})" if status_tag else f"{name} - {balance}"
                detail = f"Grant: {grant_period} | Ref: {ref_period} | Stage: {stage}"

                entries.append(
                    PluginEntry(
                        source="rh_portal",
                        member=name,
                        category="absence",
                        title=title,
                        detail=detail,
                        entry_date=entry_date,
                        priority=priority,
                        metadata={
                            "name": name,
                            "role": role,
                            "status": status_tag,
                            "balance": balance,
                            "grant_period": grant_period,
                            "ref_period": ref_period,
                            "stage": stage,
                        },
                    )
                )
            except Exception as e:
                self.log.warning(f"error parsing absence card: {e}")

        return entries

    def _parse_date_str(self, s: str) -> date | None:
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y"):
            try:
                return datetime.strptime(s.strip(), fmt).date()
            except ValueError:
                continue
        return None
