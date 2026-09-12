"""Jira Cloud backend (REST v3, HTTP Basic with an API token).

Two things about v3 that bite:
  * ``description`` and comment ``body`` must be Atlassian Document Format,
    not plain strings -- a string is rejected with a 400.
  * There is no writable status field. Moving an issue means reading its
    available transitions and POSTing the matching transition id.
"""

from __future__ import annotations

import itertools
import logging

from requests.auth import HTTPBasicAuth

import config
from adapters.ticket_base import TicketAdapter, TicketRef

log = logging.getLogger(__name__)

_fake_ids = itertools.count(101)


def _adf(text: str) -> dict:
    """Wrap plain text in Atlassian Document Format."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in (text or "").split("\n")
        if line.strip()
    ] or [{"type": "paragraph", "content": []}]
    return {"type": "doc", "version": 1, "content": paragraphs}


class JiraAdapter(TicketAdapter):
    name = "jira"
    ref_key = "jira_id"

    def missing_config(self) -> list[str]:
        required = {
            "JIRA_BASE_URL": config.JIRA_BASE_URL,
            "JIRA_EMAIL": config.JIRA_EMAIL,
            "JIRA_API_TOKEN": config.JIRA_API_TOKEN,
            "JIRA_PROJECT_KEY": config.JIRA_PROJECT_KEY,
        }
        return [key for key, value in required.items() if not value]

    # -- plumbing ---------------------------------------------------------

    @property
    def _auth(self):
        return HTTPBasicAuth(config.JIRA_EMAIL or "", config.JIRA_API_TOKEN or "")

    def _url(self, path: str) -> str:
        base = config.JIRA_BASE_URL or "https://your-domain.atlassian.net"
        return f"{base}/rest/api/3{path}"

    def _call(self, method: str, path: str, body: dict | None = None, kind: str = "") -> dict:
        return self._request(
            method,
            self._url(path),
            json_body=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            auth=self._auth,
            kind=kind,
        )

    def _dry_run_response(self, method: str, url: str, body: dict | None, kind: str) -> dict:
        """Mimic Jira's real response shapes so parsing code still runs."""
        if kind == "create":
            key = f"{config.JIRA_PROJECT_KEY}-{next(_fake_ids)}"
            return {"id": str(next(_fake_ids)), "key": key, "self": self._url(f"/issue/{key}")}
        if kind == "transitions_list":
            return {
                "transitions": [
                    {"id": "11", "name": "To Do", "to": {"name": "To Do"}},
                    {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}},
                    {"id": "31", "name": "Blocked", "to": {"name": "Blocked"}},
                    {"id": "41", "name": "Done", "to": {"name": "Done"}},
                ]
            }
        return {}

    def browse_url(self, key: str) -> str:
        base = config.JIRA_BASE_URL or "https://your-domain.atlassian.net"
        return f"{base}/browse/{key}"

    # -- contract ---------------------------------------------------------

    def create_ticket(self, item: dict) -> TicketRef:
        payload = {
            "fields": {
                "project": {"key": config.JIRA_PROJECT_KEY},
                "summary": self.build_summary(item, limit=250),
                "description": _adf(self.build_description(item)),
                "issuetype": {"name": config.JIRA_ISSUE_TYPE},
            }
        }
        if item.get("due_date"):
            payload["fields"]["duedate"] = item["due_date"]

        response = self._call("POST", "/issue", payload, kind="create")
        key = response.get("key") or response.get("id") or "UNKNOWN"
        return TicketRef(platform=self.name, id=str(key), url=self.browse_url(str(key)))

    def update_status(self, ref: TicketRef, status: str) -> bool:
        target = config.JIRA_STATUS_MAP.get(status, status)
        listing = self._call("GET", f"/issue/{ref.id}/transitions", kind="transitions_list")
        transitions = listing.get("transitions") or []

        wanted = target.strip().lower()
        match = None
        for transition in transitions:
            names = {
                str(transition.get("name", "")).strip().lower(),
                str((transition.get("to") or {}).get("name", "")).strip().lower(),
            }
            if wanted in names:
                match = transition
                break

        if match is None:
            available = [t.get("name") for t in transitions]
            log.warning(
                "jira: no transition to %r on %s (available: %s). "
                "Set JIRA_STATUS_MAP to match your workflow.",
                target,
                ref.id,
                available,
            )
            return False

        self._call(
            "POST",
            f"/issue/{ref.id}/transitions",
            {"transition": {"id": str(match["id"])}},
            kind="transition",
        )
        return True

    def add_comment(self, ref: TicketRef, text: str) -> bool:
        self._call("POST", f"/issue/{ref.id}/comment", {"body": _adf(text)}, kind="comment")
        return True
