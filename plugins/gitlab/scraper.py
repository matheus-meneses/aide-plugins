import sys
from datetime import date, datetime
from typing import ClassVar

import gitlab

from aide_sdk.base import BaseScraper
from aide_sdk.models import PluginEntry


class GitlabScraper(BaseScraper):
    name = "gitlab"
    version = "1.0.0"
    categories: ClassVar[list[str]] = ["task"]

    def validate_config(self, config: dict) -> None:
        if "base_url" not in config:
            raise ValueError("Missing 'base_url' in config")

    def scrape(self, config: dict, secrets: dict) -> list[PluginEntry]:
        base_url = config["base_url"]
        token = secrets.get("token", "")
        if not token:
            raise ValueError("Credential 'token' is required")

        self._log("Connecting to GitLab...")
        gl = gitlab.Gitlab(base_url, private_token=token, ssl_verify=False)
        gl.auth()
        username = gl.user.username
        self._log(f"Authenticated as {gl.user.name} (@{username})")

        entries: list[PluginEntry] = []
        entries.extend(self._fetch_mrs_to_review(gl, username))
        entries.extend(self._fetch_mrs_assigned(gl))
        entries.extend(self._fetch_work_items_assigned(gl))
        entries.extend(self._fetch_work_items_authored(gl))

        self._log(f"Done. {len(entries)} entries collected.")
        return entries

    def _log(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    def _fetch_mrs_to_review(self, gl: gitlab.Gitlab, username: str) -> list[PluginEntry]:
        self._log("Fetching MRs waiting for your review...")
        mrs = gl.mergerequests.list(
            reviewer_username=username,
            state="opened",
            scope="all",
            get_all=True,
        )
        self._log(f"  Found {len(mrs)} MRs to review.")
        entries = []
        for mr in mrs:
            entries.append(PluginEntry(
                source="gitlab",
                member=mr.author["name"],
                category="task",
                title=f"Review MR: {mr.title}",
                detail=f"!{mr.iid} in {mr.references['full']}",
                entry_date=self._parse_date(mr.created_at),
                priority="warning",
                metadata={
                    "type": "mr_to_review",
                    "mr_iid": mr.iid,
                    "project": mr.references["full"].rsplit("!", 1)[0],
                    "web_url": mr.web_url,
                    "author": mr.author["name"],
                    "created_at": mr.created_at,
                    "updated_at": mr.updated_at,
                },
            ))
        return entries

    def _fetch_mrs_assigned(self, gl: gitlab.Gitlab) -> list[PluginEntry]:
        self._log("Fetching MRs assigned to you...")
        mrs = gl.mergerequests.list(
            scope="assigned_to_me",
            state="opened",
            get_all=True,
        )
        self._log(f"  Found {len(mrs)} MRs assigned.")
        entries = []
        for mr in mrs:
            entries.append(PluginEntry(
                source="gitlab",
                member=mr.author["name"],
                category="task",
                title=f"Assigned MR: {mr.title}",
                detail=f"!{mr.iid} in {mr.references['full']}",
                entry_date=self._parse_date(mr.created_at),
                priority="info",
                metadata={
                    "type": "mr_assigned",
                    "mr_iid": mr.iid,
                    "project": mr.references["full"].rsplit("!", 1)[0],
                    "web_url": mr.web_url,
                    "author": mr.author["name"],
                    "created_at": mr.created_at,
                    "updated_at": mr.updated_at,
                },
            ))
        return entries

    def _fetch_work_items_assigned(self, gl: gitlab.Gitlab) -> list[PluginEntry]:
        self._log("Fetching work items assigned to you...")
        issues = gl.issues.list(
            scope="assigned_to_me",
            state="opened",
            get_all=True,
        )
        self._log(f"  Found {len(issues)} work items assigned.")
        entries = []
        for issue in issues:
            entries.append(PluginEntry(
                source="gitlab",
                member=issue.author["name"],
                category="task",
                title=f"Work Item: {issue.title}",
                detail=f"#{issue.iid} in {issue.references['full']}",
                entry_date=self._parse_date(issue.created_at),
                priority="info",
                metadata={
                    "type": "work_item_assigned",
                    "issue_iid": issue.iid,
                    "project": issue.references["full"].rsplit("#", 1)[0],
                    "web_url": issue.web_url,
                    "author": issue.author["name"],
                    "labels": issue.labels,
                    "created_at": issue.created_at,
                    "updated_at": issue.updated_at,
                },
            ))
        return entries

    def _fetch_work_items_authored(self, gl: gitlab.Gitlab) -> list[PluginEntry]:
        self._log("Fetching work items authored by you...")
        issues = gl.issues.list(
            scope="created_by_me",
            state="opened",
            get_all=True,
        )
        self._log(f"  Found {len(issues)} work items authored.")
        entries = []
        for issue in issues:
            assignees = issue.assignees or []
            member = assignees[0]["name"] if assignees else "unassigned"
            entries.append(PluginEntry(
                source="gitlab",
                member=member,
                category="task",
                title=f"Authored Item: {issue.title}",
                detail=f"#{issue.iid} in {issue.references['full']}",
                entry_date=self._parse_date(issue.created_at),
                priority="info",
                metadata={
                    "type": "work_item_authored",
                    "issue_iid": issue.iid,
                    "project": issue.references["full"].rsplit("#", 1)[0],
                    "web_url": issue.web_url,
                    "assignees": [a["name"] for a in assignees],
                    "labels": issue.labels,
                    "created_at": issue.created_at,
                    "updated_at": issue.updated_at,
                },
            ))
        return entries

    def _parse_date(self, iso_str: str) -> date:
        try:
            return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            return date.today()
