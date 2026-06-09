import sys
from datetime import date, datetime
from typing import ClassVar

import requests
import urllib3

from aide_sdk.base import BaseScraper
from aide_sdk.models import PluginEntry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class JiraScraper(BaseScraper):
    name = "jira"
    version = "1.0.0"
    categories: ClassVar[list[str]] = ["task", "metric"]

    def validate_config(self, config: dict) -> None:
        if "base_url" not in config:
            raise ValueError("Missing 'base_url' in config")
        if "queries" not in config:
            raise ValueError("Missing 'queries' in config")

    def scrape(self, config: dict, secrets: dict) -> list[PluginEntry]:
        base_url = config["base_url"].rstrip("/")
        email = secrets.get("email", "")
        token = secrets.get("token", "")
        if not email or not token:
            raise ValueError("Credentials 'email' and 'token' are required")

        self._log(f"Connecting to Jira at {base_url}...")
        session = requests.Session()
        session.auth = (email, token)
        session.verify = False
        session.headers["Accept"] = "application/json"

        myself = session.get(f"{base_url}/rest/api/3/myself")
        myself.raise_for_status()
        display_name = myself.json().get("displayName", email)
        self._log(f"Authenticated as {display_name}")

        queries_raw = config.get("queries", [])
        if isinstance(queries_raw, str):
            import json
            s = queries_raw.strip()
            if not s:
                queries_raw = []
            elif s.startswith("["):
                queries_raw = json.loads(s)
            else:
                queries_raw = [s]

        queries = []
        for i, q in enumerate(queries_raw):
            if isinstance(q, str):
                queries.append({"name": f"query_{i+1}", "jql": q, "mode": "items"})
            else:
                queries.append(q)

        entries: list[PluginEntry] = []

        for q in queries:
            name = q.get("name", "unnamed")
            jql = q.get("jql", "")
            mode = q.get("mode", "items")

            if not jql:
                self._log(f"  Skipping '{name}': empty JQL")
                continue

            self._log(f"  Running query: {name} (mode={mode})...")

            if mode == "metric":
                entries.extend(self._run_metric_query(session, base_url, name, jql))
            else:
                entries.extend(self._run_items_query(session, base_url, name, jql))

        self._log(f"Done. {len(entries)} entries collected.")
        return entries

    def _run_items_query(
        self, session: requests.Session, base_url: str, name: str, jql: str
    ) -> list[PluginEntry]:
        entries = []
        next_page_token = None

        while True:
            body = {
                "jql": jql,
                "maxResults": 100,
                "fields": [
                    "summary", "assignee", "reporter", "status", "priority",
                    "created", "updated", "issuetype", "project",
                ],
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token

            resp = session.post(f"{base_url}/rest/api/3/search/jql", json=body)
            if not resp.ok:
                self._log(f"    Jira error {resp.status_code}: {resp.text[:500]}")
                resp.raise_for_status()
            data = resp.json()

            issues = data.get("issues", [])
            for issue in issues:
                entries.append(self._issue_to_entry(base_url, name, issue))

            if data.get("isLast", True) or not issues:
                break
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        self._log(f"    {name}: {len(entries)} tickets")
        return entries

    def _run_metric_query(
        self, session: requests.Session, base_url: str, name: str, jql: str
    ) -> list[PluginEntry]:
        resp = session.post(
            f"{base_url}/rest/api/3/search/jql",
            json={"jql": jql, "maxResults": 0},
        )
        if not resp.ok:
            self._log(f"    Jira error {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
        total = resp.json().get("total", 0)
        self._log(f"    {name}: count={total}")

        return [
            PluginEntry(
                source="jira",
                member="",
                category="metric",
                title=name,
                detail=str(total),
                entry_date=date.today(),
                priority="info",
                metadata={
                    "mode": "metric",
                    "metric_value": total,
                    "jql": jql,
                },
            )
        ]

    def _issue_to_entry(self, base_url: str, query_name: str, issue: dict) -> PluginEntry:
        fields = issue.get("fields", {})
        key = issue.get("key", "")
        summary = fields.get("summary", "")
        assignee = fields.get("assignee") or {}
        reporter = fields.get("reporter") or {}
        status = fields.get("status", {}).get("name", "")
        priority = fields.get("priority", {}).get("name", "")
        created = fields.get("created", "")
        project = fields.get("project", {}).get("key", "")

        member = assignee.get("displayName", "unassigned")
        browse_url = f"{base_url}/browse/{key}"

        entry_priority = "info"
        if priority and priority.lower() in ("highest", "critical", "blocker"):
            entry_priority = "critical"
        elif priority and priority.lower() in ("high",):
            entry_priority = "warning"

        return PluginEntry(
            source="jira",
            member=member,
            category="task",
            title=f"[{key}] {summary}",
            detail=f"{project} | {status} | {priority}",
            entry_date=self._parse_date(created),
            priority=entry_priority,
            metadata={
                "mode": "items",
                "web_url": browse_url,
                "query_name": query_name,
                "key": key,
                "status": status,
                "priority": priority,
                "reporter": reporter.get("displayName", ""),
                "created": created,
            },
        )

    def _parse_date(self, iso_str: str) -> date:
        try:
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError, TypeError):
            return date.today()

    def _log(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)
