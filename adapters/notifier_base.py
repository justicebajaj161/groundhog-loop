"""The contract every notification backend implements.

Message *content* is built here, once, in platform-neutral form. Each adapter
decides how to render it -- Slack as Block Kit with real buttons, Teams as an
Adaptive Card -- so wording stays consistent across platforms and only the
rendering differs.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from adapters._http import AdapterBase, AdapterError  # noqa: F401  (re-exported)

# The structured quick-replies. Pressing one of these sets status directly and
# must never reach the LLM -- see pipeline.apply_structured_reply.
STATUS_ACTIONS: list[tuple[str, str]] = [
    ("done", "Done"),
    ("in_progress", "In progress"),
    ("blocked", "Blocked"),
]


@dataclass
class Delivery:
    """Outcome of one send on one platform."""

    platform: str
    target: str
    ok: bool
    detail: str | None = None

    def __str__(self) -> str:
        mark = "ok" if self.ok else "FAILED"
        return f"{self.platform} -> {self.target} [{mark}]" + (
            f" {self.detail}" if self.detail else ""
        )


class NotifierAdapter(AdapterBase):
    name: str = "notifier"
    # Whether this backend can render real interactive buttons.
    supports_buttons: bool = False

    @abstractmethod
    def post_message(self, channel: str, text: str, *, item: dict | None = None) -> Delivery:
        """Post to a shared channel."""

    @abstractmethod
    def dm_user(self, user: str, text: str, *, item: dict | None = None) -> Delivery:
        """Direct-message one person."""

    def default_channel(self) -> str:
        return ""

    # -- message copy, shared across platforms ---------------------------

    @staticmethod
    def _ticket_line(item: dict) -> str:
        refs = item.get("ticket_refs") or {}
        parts = [
            f"{key.replace('_id', '').title()}: {value}"
            for key, value in refs.items()
            if key.endswith("_id") and value
        ]
        return " | ".join(parts)

    @classmethod
    def build_nudge(cls, item: dict, days_left: int | None = None) -> str:
        """The DM asking an owner where a due item stands."""
        text = item.get("item_text", "an action item")
        if days_left is None or item.get("due_date") is None:
            timing = "This is still open."
        elif days_left < 0:
            timing = f"This was due {abs(days_left)} day(s) ago ({item['due_date']})."
        elif days_left == 0:
            timing = f"This is due today ({item['due_date']})."
        else:
            timing = f"This is due in {days_left} day(s), on {item['due_date']}."

        lines = [f"Quick check on your action item from {item.get('source_meeting') or 'the retro'}:",
                 "", f"*{text}*", "", timing]

        if (item.get("recurrence_count") or 1) > 1:
            lines += ["", f"Heads up: this is occurrence #{item['recurrence_count']} of a recurring issue."]

        tickets = cls._ticket_line(item)
        if tickets:
            lines += ["", tickets]

        lines += ["", "Where does it stand?"]
        return "\n".join(lines)

    @classmethod
    def build_assignment(cls, item: dict) -> str:
        """The DM sent when an item is first captured and filed."""
        lines = [
            f"New action item from *{item.get('source_meeting') or 'the retro'}*, "
            "assigned to you:",
            "",
            f"*{item.get('item_text', '')}*",
        ]
        if item.get("due_date"):
            lines.append(f"Due {item['due_date']}.")
        else:
            lines.append("No due date was agreed -- reply with one if you have it.")

        recurrence = item.get("recurrence") or {}
        if recurrence.get("is_recurring"):
            lines += [
                "",
                f"Note: this is occurrence #{recurrence.get('count')} of a recurring issue "
                f"(matched {recurrence.get('best_score')} against "
                f"\"{recurrence.get('matched_text')}\").",
            ]

        tickets = cls._ticket_line(item)
        if tickets:
            lines += ["", f"Filed as {tickets}"]

        lines += ["", "You'll get a nudge as the date approaches. Status?"]
        return "\n".join(lines)

    @classmethod
    def build_escalation(cls, item: dict, days_overdue: int) -> str:
        """The channel post when something has gone stale."""
        lines = [
            "*Overdue action item*",
            "",
            f"*{item.get('item_text', '')}*",
            f"Owner: {item.get('owner') or 'unassigned'}",
            f"Due {item.get('due_date')} -- {days_overdue} day(s) overdue.",
            f"From: {item.get('source_meeting') or 'unknown meeting'}",
        ]
        if item.get("blocker_note"):
            lines.append(f"Last reported blocker: {item['blocker_note']}")
        tickets = cls._ticket_line(item)
        if tickets:
            lines.append(tickets)
        return "\n".join(lines)

    @classmethod
    def build_recurrence_alert(cls, item: dict, recurrence: dict) -> str:
        """The channel post when an item proves to be a repeat offender."""
        lines = [
            f"*This keeps coming back -- occurrence #{recurrence.get('count')}*",
            "",
            f"*{item.get('item_text', '')}*",
            "",
            f"The team has now agreed to this {recurrence.get('count')} times across "
            f"separate meetings. Most recent match scored "
            f"{recurrence.get('best_score')} against:",
            f'  "{recurrence.get("matched_text")}"',
        ]
        prior = recurrence.get("prior_refs") or []
        if prior:
            lines += ["", "Previously filed as: " + ", ".join(prior)]
        lines += [
            "",
            "Recurring at this rate usually means the fix addressed a symptom. "
            "Worth a root-cause look rather than another ticket.",
        ]
        return "\n".join(lines)
