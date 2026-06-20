import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from aide_sdk.base import BaseScraper
from aide_sdk.models import PluginEntry

OUTLOOK_DOMAIN = "outlook.office.com"
CALENDAR_DOMAIN = "outlook.cloud.microsoft"
LOGIN_INDICATORS = ["login.microsoftonline.com", "login.live.com"]


def _sessions_dir() -> Path:
    aide_home = os.environ.get("AIDE_HOME") or str(Path.home() / ".aide")
    p = Path(aide_home) / "plugins" / "outlook" / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


class OutlookScraper(BaseScraper):
    name = "outlook"
    version = "1.0.0"
    categories: ClassVar[list[str]] = ["event", "metric"]

    def validate_config(self, config: dict) -> None:
        pass

    def scrape(self, config: dict, secrets: dict) -> list[PluginEntry]:
        days_ahead = int(config.get("calendar_days_ahead", 7))
        self._allowed_calendars = [c.lower() for c in config.get("calendars", [])]
        self._session_file = _sessions_dir() / "outlook.json"

        with sync_playwright() as p:
            context, page, browser = self._authenticate(p)

            self._log("Authenticated. Extracting data...")
            entries: list[PluginEntry] = []
            entries.extend(self._scrape_calendar(page, context, days_ahead))
            entries.extend(self._scrape_mail_count(page, context))

            self._log(f"Done. {len(entries)} entries collected.")
            context.close()
            browser.close()
            return entries

    def _authenticate(self, p: Playwright):
        has_session = self._session_file.exists()

        self._log("Starting headless browser...")
        browser = p.chromium.launch(headless=True)
        if has_session:
            context = browser.new_context(
                storage_state=str(self._session_file),
                ignore_https_errors=True,
                service_workers="block",
            )
        else:
            context = browser.new_context(ignore_https_errors=True, service_workers="block")

        page = context.new_page()
        page.goto(f"https://{OUTLOOK_DOMAIN}/mail", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        if not self._needs_login(page):
            self._save_session(context)
            return context, page, browser

        self._log("Session expired. Attempting headless auto-auth...")
        self._try_auto_select_account(page)
        try:
            page.wait_for_url(f"**/{OUTLOOK_DOMAIN}/**", timeout=30000)
            page.wait_for_timeout(3000)
            if not self._needs_login(page):
                self._save_session(context)
                self._log("Headless auto-auth successful!")
                return context, page, browser
        except Exception:
            pass

        self._log("Headless auth failed. Opening visible browser for manual login...")
        page.close()
        context.close()
        browser.close()

        browser = p.chromium.launch(headless=False)
        if has_session:
            context = browser.new_context(
                storage_state=str(self._session_file),
                ignore_https_errors=True,
                service_workers="block",
            )
        else:
            context = browser.new_context(ignore_https_errors=True, service_workers="block")

        page = context.new_page()
        page.goto(f"https://{OUTLOOK_DOMAIN}/mail", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        if self._needs_login(page):
            self._try_auto_select_account(page)
            page.wait_for_url(f"**/{OUTLOOK_DOMAIN}/**", timeout=300000)
            page.wait_for_timeout(5000)

        self._save_session(context)
        self._log("Login successful!")
        return context, page, browser

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

    def _log(self, msg: str) -> None:
        self.log.debug(msg)

    def _needs_login(self, page: Page) -> bool:
        url = page.url
        return any(indicator in url for indicator in LOGIN_INDICATORS) or OUTLOOK_DOMAIN not in url

    def _save_session(self, context: BrowserContext) -> None:
        context.storage_state(path=str(self._session_file))

    def _scrape_calendar(self, current_page: Page, context: BrowserContext, days_ahead: int) -> list[PluginEntry]:
        self._log("  Fetching calendar...")
        page = context.new_page()

        captured_events = []
        folder_id_to_name: dict[str, str] = {}

        def on_response(response):
            url = response.url
            if response.status != 200:
                return

            if "startupdata.ashx" in url and "Calendar" in url:
                try:
                    data = json.loads(response.body())
                    cal_folders = data.get("getCalendarFolders", {}).get("CalendarFolders", [])
                    for f in cal_folders:
                        fid = f.get("FolderId", {})
                        folder_id = fid.get("Id", "") if isinstance(fid, dict) else ""
                        name = f.get("DisplayName") or f.get("Name") or ""
                        if folder_id and name:
                            folder_id_to_name[folder_id] = name
                except Exception:
                    pass

            is_calendar = "GetCalendarView" in url or "calendarView" in url or "calendar/events" in url
            if is_calendar:
                try:
                    body = response.body()
                    data = json.loads(body)
                    if isinstance(data, dict):
                        items = data.get("Body", {}).get("Items", [])
                        if items:
                            captured_events.extend(items)
                        else:
                            events = data.get("value", data.get("Value", []))
                            if events and isinstance(events, list):
                                captured_events.extend(events)
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(f"https://{CALENDAR_DOMAIN}/calendar/view/week", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)

        if self._allowed_calendars and not folder_id_to_name:
            self._fetch_calendar_folders(page, folder_id_to_name)

        next_btn = page.locator(
            "button[aria-label*='semana seguinte'], button[aria-label*='next week'], button[aria-label*='Next week']"
        )
        if next_btn.count() > 0:
            next_btn.first.click()
            page.wait_for_timeout(8000)

        page.close()

        seen_uids = set()
        deduped = []
        for ev in captured_events:
            uid = ev.get("UID") or ev.get("ItemId", {}).get("Id", "")
            if uid and uid in seen_uids:
                continue
            seen_uids.add(uid)
            deduped.append(ev)
        captured_events = deduped

        if self._allowed_calendars:
            captured_events = self._filter_by_calendar(captured_events, folder_id_to_name)

        self._log(f"    Calendar: {len(captured_events)} events captured")
        entries = []
        now = datetime.now().astimezone()
        today = now.date()
        cutoff_date = today + timedelta(days=days_ahead)
        for event in captured_events:
            if event.get("IsCancelled") or event.get("isCancelled"):
                continue
            subject = event.get("Subject") or event.get("subject") or "(No subject)"
            start = event.get("Start") or event.get("start")
            end_time = event.get("End") or event.get("end")

            organizer_obj = event.get("Organizer") or event.get("organizer") or {}
            mailbox = (
                organizer_obj.get("Mailbox")
                or organizer_obj.get("EmailAddress")
                or organizer_obj.get("emailAddress")
                or {}
            )
            organizer_name = (
                mailbox.get("Name") or mailbox.get("name") or mailbox.get("Address") or mailbox.get("address") or ""
            )

            start_dt = self._parse_graph_datetime(start)
            end_dt = self._parse_graph_datetime(end_time)

            if start_dt and start_dt.date() < today:
                continue
            if start_dt and start_dt.date() > cutoff_date:
                continue
            if end_dt and end_dt < now:
                continue

            start_str_display = start_dt.strftime("%H:%M") if start_dt else ""
            duration = ""
            if start_dt and end_dt:
                mins = int((end_dt - start_dt).total_seconds() / 60)
                duration = f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"

            entry_date = start_dt.date() if start_dt else date.today()

            entries.append(
                PluginEntry(
                    source="outlook",
                    member=organizer_name,
                    category="event",
                    title=f"Meeting: {subject}",
                    detail=f"{start_str_display} ({duration})" if duration else start_str_display,
                    entry_date=entry_date,
                    priority="info",
                    metadata={"mode": "items"},
                )
            )
        return entries

    def _scrape_mail_count(self, current_page: Page, context: BrowserContext) -> list[PluginEntry]:
        self._log("  Fetching mail count...")
        page = context.new_page()

        captured_count = [0]
        found = [False]

        def on_response(response):
            if found[0]:
                return
            url = response.url
            if response.status != 200:
                return
            is_mail_folder = "FindFolder" in url or "GetFolder" in url or "mailFolders" in url or "MailFolders" in url
            if is_mail_folder:
                try:
                    body = response.body()
                    data = json.loads(body)
                    count = self._extract_unread_from_response(data)
                    if count is not None:
                        captured_count[0] = count
                        found[0] = True
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(f"https://{OUTLOOK_DOMAIN}/mail", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)
        page.close()

        unread = captured_count[0]
        self._log(f"    Inbox unread: {unread}")
        return [
            PluginEntry(
                source="outlook",
                member="",
                category="metric",
                title="Inbox Unread",
                detail=str(unread),
                entry_date=date.today(),
                priority="info",
                metadata={"mode": "metric", "metric_value": unread},
            )
        ]

    def _fetch_calendar_folders(self, page: Page, folder_id_to_name: dict[str, str]) -> None:
        self._log("    Fetching calendar folders via Graph API...")
        try:
            result = page.evaluate("""
                async () => {
                    try {
                        const resp = await fetch('/api/v2.0/me/calendars?$select=Id,Name', {credentials: 'include'});
                        if (resp.ok) return await resp.json();
                        const resp2 = await fetch('https://outlook.office.com/api/v2.0/me/calendars?$select=Id,Name', {credentials: 'include'});
                        if (resp2.ok) return await resp2.json();
                        return null;
                    } catch(e) { return null; }
                }
            """)
            if result and "value" in result:
                for cal in result["value"]:
                    cal_id = cal.get("Id") or cal.get("id") or ""
                    cal_name = cal.get("Name") or cal.get("name") or ""
                    if cal_id and cal_name:
                        folder_id_to_name[cal_id] = cal_name
                self._log(f"    Fetched {len(folder_id_to_name)} calendars")
        except Exception as e:
            self.log.error(f"failed to fetch calendar folders: {e}")

    def _filter_by_calendar(self, events: list[dict], folder_id_to_name: dict[str, str]) -> list[dict]:
        if folder_id_to_name:
            allowed_ids = {fid for fid, fname in folder_id_to_name.items() if fname.lower() in self._allowed_calendars}
            if allowed_ids:
                before = len(events)
                events = [ev for ev in events if ev.get("ParentFolderId", {}).get("Id", "") in allowed_ids]
                self._log(f"    Calendar filter: {before} -> {len(events)}")
                return events

        self._log("    Calendar filter: folder mapping unavailable, using heuristic")
        before = len(events)
        events = [ev for ev in events if not self._is_shared_calendar_event(ev)]
        self._log(f"    Heuristic filter: {before} -> {len(events)}")
        return events

    def _is_shared_calendar_event(self, event: dict) -> bool:
        calendar_name = event.get("Calendar", {}).get("Name", "") if isinstance(event.get("Calendar"), dict) else ""
        if calendar_name and calendar_name.lower() not in self._allowed_calendars:
            return True
        is_organizer = event.get("IsOrganizer", event.get("isOrganizer"))
        response_status = event.get("ResponseStatus", {}).get("Response", "") or event.get("responseStatus", {}).get(
            "response", ""
        )
        return bool(response_status.lower() in ("none", "notresponded") and is_organizer is False)

    def _extract_unread_from_response(self, data: dict) -> int | None:
        body = data.get("Body", {})
        if isinstance(body, dict):
            resp_items = body.get("ResponseMessages", {}).get("Items", [])
            for item in resp_items:
                folders = item.get("Folders", [])
                if len(folders) == 1 and "UnreadCount" in folders[0]:
                    return int(folders[0]["UnreadCount"])
                for f in folders:
                    name = (f.get("DisplayName") or "").lower()
                    if name in ("inbox", "caixa de entrada"):
                        count = f.get("UnreadCount")
                        if count is not None:
                            return int(count)

        if "value" in data:
            for folder in data["value"]:
                name = (folder.get("DisplayName") or folder.get("displayName") or "").lower()
                if name in ("inbox", "caixa de entrada"):
                    count = folder.get("UnreadItemCount") or folder.get("unreadItemCount")
                    if count is not None:
                        return int(count)

        return None

    def _parse_graph_datetime(self, dt_obj) -> datetime | None:
        try:
            if isinstance(dt_obj, str):
                parsed = datetime.fromisoformat(dt_obj.rstrip("Z"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone()
            if isinstance(dt_obj, dict):
                raw = dt_obj.get("dateTime") or dt_obj.get("DateTime") or ""
                if not raw:
                    return None
                parsed = datetime.fromisoformat(raw.rstrip("Z"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone()
            return None
        except (ValueError, TypeError):
            return None

    def render(self, heading: str, items: list[dict], config: dict) -> list[str]:
        if heading == "event":
            return _render_calendar(items)
        if heading == "metric":
            return _render_metrics(items)
        return _render_default(items)


def _parse_detail(detail: str) -> tuple[int, int]:
    """Returns (start_minutes, duration_minutes) or (-1, 0) if unparseable."""
    if not detail or len(detail) < 5:
        return -1, 0
    time_part = detail
    dur_part = ""
    m = re.match(r"(\d{2}:\d{2})\s*(?:\(([^)]+)\))?", detail)
    if not m:
        return -1, 0
    time_part = m.group(1)
    dur_part = m.group(2) or ""
    h, mi = map(int, time_part.split(":"))
    start = h * 60 + mi
    dur = 0
    dh = re.search(r"(\d+)h", dur_part)
    dm = re.search(r"(\d+)m", dur_part)
    if dh:
        dur += int(dh.group(1)) * 60
    if dm:
        dur += int(dm.group(1))
    if dur == 0:
        dur = 30
    return start, dur


def _render_calendar(items: list[dict]) -> list[str]:
    today_str = date.today().isoformat()
    now = datetime.now()

    future = [i for i in items if (i.get("entry_date") or "") >= today_str]
    future.sort(
        key=lambda i: (
            i.get("entry_date", ""),
            _parse_detail(i.get("detail", ""))[0],
        )
    )

    by_day: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in future:
        d = item.get("entry_date") or today_str
        if d not in by_day:
            by_day[d] = []
            order.append(d)
        by_day[d].append(item)

    if today_str not in by_day:
        by_day[today_str] = []
        order = [today_str, *order]

    lines: list[str] = []
    for day in order:
        day_items = by_day[day]
        label = _day_label(day, today_str)
        lines.append(" │")
        lines.append(f" │  ── {label} {'─' * max(0, 45 - len(label))}")

        if not day_items:
            lines.append(" │    (no meetings)")
            continue

        conflicts = _detect_conflicts(day_items)

        for idx, item in enumerate(day_items):
            title = re.sub(r"^Meeting:\s*", "", item.get("title", ""))
            member = item.get("member", "")
            detail = item.get("detail", "")

            start, dur = _parse_detail(detail)
            time_str = f"{start // 60:02d}:{start % 60:02d}" if start >= 0 else "     "
            dur_str = ""
            dm = re.search(r"\(([^)]+)\)", detail)
            if dm:
                dur_str = dm.group(1)

            status = ""
            if day == today_str and start >= 0:
                slot_start = now.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
                slot_end = slot_start + timedelta(minutes=dur)
                if now > slot_end:
                    status = " ✓"
                elif now >= slot_start:
                    status = " ●"

            conflict = " ⚠" if conflicts[idx] else ""

            if len(title) > 40:
                title = title[:37] + "..."
            if len(member) > 22:
                member = member[:19] + "..."

            pad = " " * max(0, 40 - len(title))
            dur_col = f"  {dur_str}" if dur_str else ""
            lines.append(f" │    {time_str}  {title}{pad}  {member:<22}{dur_col}{conflict}{status}")

    return lines


def _detect_conflicts(items: list[dict]) -> list[bool]:
    intervals = []
    for item in items:
        s, d = _parse_detail(item.get("detail", ""))
        intervals.append((s, s + d) if s >= 0 else (-1, -1))
    result = [False] * len(items)
    for i in range(len(intervals)):
        if intervals[i][0] < 0:
            continue
        for j in range(i + 1, len(intervals)):
            if intervals[j][0] < 0:
                continue
            if intervals[i][0] < intervals[j][1] and intervals[j][0] < intervals[i][1]:
                result[i] = True
                result[j] = True
    return result


def _day_label(day: str, today: str) -> str:
    try:
        t = datetime.strptime(day, "%Y-%m-%d")
        name = t.strftime("%a")
        date_part = t.strftime("%d/%m")
        if day == today:
            return f"Today ({name} {date_part})"
        return f"{name} {date_part}"
    except ValueError:
        return day


def _render_metrics(items: list[dict]) -> list[str]:
    lines: list[str] = [" │"]
    for item in items:
        title = item.get("title", "")
        detail = item.get("detail", "")
        lines.append(f" │    {title}: {detail}")
    return lines


def _render_default(items: list[dict]) -> list[str]:
    lines: list[str] = []
    for item in items:
        title = re.sub(r"^Meeting:\s*", "", item.get("title", ""))
        if len(title) > 50:
            title = title[:47] + "..."
        lines.append(f" │    {title}")
    return lines
