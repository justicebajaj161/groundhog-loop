"""ClickUp backend (API v2).

Two quirks worth knowing:
  * The auth header takes the bare token -- ``Authorization: pk_...``. Adding
    a ``Bearer`` prefix, as almost every other API expects, returns 401.
  * Dates are Unix milliseconds, not ISO strings.

v2 is the current task API; there is no v3 equivalent (api.clickup.com/api/v3
returns 404).
"""

from __future__ import annotations

import itertools
import logging
from datetime import date, datetime, time, timezone

import config
from adapters.ticket_base import TicketAdapter, TicketRef

log = logging.getLogger(__name__)

_fake_ids = itertools.count(1)


def _epoch_ms(iso_date: str | None) -> int | None:
    """ISO date -> Unix milliseconds, which is what ClickUp stores."""
    if not iso_date:
        return None
    try:
        parsed = date.fromisoformat(iso_date[:10])
    except ValueError:
        return None
    moment = datetime.combine(parsed, time(17, 0), tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


class ClickUpAdapter(TicketAdapter):
    name = "clickup"
    ref_key = "clickup_id"

    def missing_config(self) -> list[str]:
        required = {
            "CLICKUP_API_TOKEN": config.CLICKUP_API_TOKEN,
            "CLICKUP_LIST_ID": config.CLICKUP_LIST_ID,
        }
        return [key for key, value in required.items() if not value]

    # -- plumbing ---------------------------------------------------------

    @property
    def _headers(self) -> dict:
        # Bare token, no "Bearer" -- ClickUp rejects the prefix.
        return {
            "Authorization": config.CLICKUP_API_TOKEN or "",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{config.CLICKUP_BASE_URL}{path}"

    def _call(self, method: str, path: str, body: dict | None = None, kind: str = "") -> dict:
        return self._request(
            method, self._url(path), json_body=body, headers=self._headers, kind=kind
        )

    def _dry_run_response(self, method: str, url: str, body: dict | None, kind: str) -> dict:
        if kind == "create":
            task_id = f"86a{next(_fake_ids):04d}k"
            return {"id": task_id, "url": f"https://app.clickup.com/t/{task_id}"}
        return {}

    # -- contract ---------------------------------------------------------

    def create_ticket(self, item: dict) -> TicketRef:
        payload: dict = {
            "name": self.build_summary(item, limit=255),
            "description": self.build_description(item),
        }
        due_ms = _epoch_ms(item.get("due_date"))
        if due_ms:
            payload["due_date"] = due_ms
            payload["due_date_time"] = False

        status = config.CLICKUP_STATUS_MAP.get(item.get("status") or "pending")
        if status:
            payload["status"] = status

        list_id = config.CLICKUP_LIST_ID or "<CLICKUP_LIST_ID>"
        response = self._call("POST", f"/list/{list_id}/task", payload, kind="create")
        task_id = str(response.get("id") or "UNKNOWN")
        return TicketRef(
            platform=self.name,
            id=task_id,
            url=response.get("url") or f"https://app.clickup.com/t/{task_id}",
        )

    def update_status(self, ref: TicketRef, status: str) -> bool:
        target = config.CLICKUP_STATUS_MAP.get(status, status)
        self._call("PUT", f"/task/{ref.id}", {"status": target}, kind="update_status")
        return True

    def add_comment(self, ref: TicketRef, text: str) -> bool:
        self._call(
            "POST",
            f"/task/{ref.id}/comment",
            {"comment_text": text, "notify_all": False},
            kind="comment",
        )
        return True
