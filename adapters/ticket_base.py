"""The contract every ticketing backend implements."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import asdict, dataclass

from adapters._http import AdapterBase, AdapterError  # noqa: F401  (re-exported)


@dataclass
class TicketRef:
    """A ticket's identity on one platform."""

    platform: str
    id: str
    url: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        return f"{self.platform}:{self.id}"


class TicketAdapter(AdapterBase):
    """Create and maintain a ticket on one platform.

    ``item`` is an action-item dict as stored by storage.py, optionally
    carrying a ``recurrence`` key that adapters may fold into the ticket body.
    """

    name: str = "ticket"
    # Key used inside items.ticket_refs, e.g. "jira_id".
    ref_key: str = "ticket_id"

    @abstractmethod
    def create_ticket(self, item: dict) -> TicketRef:
        """File a new ticket and return its reference."""

    @abstractmethod
    def update_status(self, ref: TicketRef, status: str) -> bool:
        """Move the ticket to the platform's equivalent of ``status``."""

    @abstractmethod
    def add_comment(self, ref: TicketRef, text: str) -> bool:
        """Append a comment to the ticket."""

    # -- helpers shared by concrete adapters ------------------------------

    @staticmethod
    def build_description(item: dict) -> str:
        """Human-readable ticket body, including recurrence provenance."""
        lines = [item.get("item_text") or item.get("item") or ""]

        context = item.get("source_context")
        if context:
            lines += ["", f'From the transcript: "{context}"']

        meta = []
        if item.get("source_meeting"):
            meta.append(f"Meeting: {item['source_meeting']}")
        if item.get("owner"):
            meta.append(f"Owner: {item['owner']}")
        if item.get("due_date"):
            meta.append(f"Due: {item['due_date']}")
        if meta:
            lines += [""] + meta

        recurrence = item.get("recurrence") or {}
        if recurrence.get("is_recurring"):
            prior = recurrence.get("prior_refs") or []
            lines += [
                "",
                f"RECURRING ISSUE -- occurrence #{recurrence.get('count')} "
                f"(group {recurrence.get('group_id')}).",
                f"Semantic match {recurrence.get('best_score')} against: "
                f"\"{recurrence.get('matched_text')}\"",
            ]
            if prior:
                lines.append("Previously filed as: " + ", ".join(prior))
            lines.append(
                "This has been agreed before and has come back. Consider treating the "
                "underlying cause rather than the symptom."
            )

        lines += ["", "Filed automatically by Groundhog Loop."]
        return "\n".join(lines)

    @staticmethod
    def build_summary(item: dict, limit: int = 200) -> str:
        text = (item.get("item_text") or item.get("item") or "action item").strip()
        recurrence = item.get("recurrence") or {}
        if recurrence.get("is_recurring"):
            text = f"[Recurring x{recurrence.get('count')}] {text}"
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return text
