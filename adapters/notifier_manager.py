"""Fans notifications out to every enabled platform at once.

Identical pattern to TicketManager: config decides which backends exist,
callers never name one, and a failure on one platform never suppresses
delivery on another.
"""

from __future__ import annotations

import logging

import config
from adapters.notifier_base import Delivery, NotifierAdapter
from adapters.slack_adapter import SlackAdapter
from adapters.teams_adapter import TeamsAdapter

log = logging.getLogger(__name__)

# Adding a platform is one entry here plus one file.
REGISTRY: dict[str, type[NotifierAdapter]] = {
    "slack": SlackAdapter,
    "teams": TeamsAdapter,
}


class NotifierManager:
    def __init__(self, enabled: list[str] | None = None):
        names = enabled if enabled is not None else config.ENABLED_NOTIFIER_ADAPTERS
        self.adapters: list[NotifierAdapter] = []
        for name in names:
            adapter_cls = REGISTRY.get(name)
            if adapter_cls is None:
                log.warning(
                    "unknown notifier adapter %r (known: %s) -- skipping",
                    name,
                    ", ".join(sorted(REGISTRY)),
                )
                continue
            self.adapters.append(adapter_cls())

    def __bool__(self) -> bool:
        return bool(self.adapters)

    def describe(self) -> list[str]:
        return [f"{a.name} [{a.status_note()}]" for a in self.adapters]

    # -- fan-out ----------------------------------------------------------

    def post_message(
        self, channel: str | None, text: str, *, item: dict | None = None
    ) -> list[Delivery]:
        """Post to the team channel on every platform.

        ``channel=None`` means "each platform's own configured team channel",
        which is the usual case -- the Slack channel and the Teams channel are
        different strings for the same audience.
        """
        deliveries = []
        for adapter in self.adapters:
            target = channel or adapter.default_channel()
            try:
                deliveries.append(adapter.post_message(target, text, item=item))
            except Exception as exc:
                log.error("%s: post_message failed: %s", adapter.name, exc)
                deliveries.append(Delivery(adapter.name, target, ok=False, detail=str(exc)))
        return deliveries

    def dm_user(self, user: str, text: str, *, item: dict | None = None) -> list[Delivery]:
        deliveries = []
        for adapter in self.adapters:
            try:
                deliveries.append(adapter.dm_user(user, text, item=item))
            except Exception as exc:
                log.error("%s: dm_user failed: %s", adapter.name, exc)
                deliveries.append(Delivery(adapter.name, user, ok=False, detail=str(exc)))
        return deliveries

    # -- convenience wrappers used by the pipeline and scheduler ----------

    def nudge(self, item: dict, days_left: int | None = None) -> list[Delivery]:
        owner = item.get("owner")
        text = NotifierAdapter.build_nudge(item, days_left)
        if not owner:
            return self.post_message(None, f"(unassigned)\n{text}", item=item)
        return self.dm_user(owner, text, item=item)

    def escalate(self, item: dict, days_overdue: int) -> list[Delivery]:
        return self.post_message(None, NotifierAdapter.build_escalation(item, days_overdue))

    def recurrence_alert(self, item: dict, recurrence: dict) -> list[Delivery]:
        return self.post_message(None, NotifierAdapter.build_recurrence_alert(item, recurrence))
