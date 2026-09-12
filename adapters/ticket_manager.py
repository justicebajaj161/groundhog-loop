"""Fans ticket operations out to every enabled platform at once.

Callers never name a platform. Which backends are live is decided entirely
by ENABLED_TICKET_ADAPTERS, and each platform's ticket id is stored under its
own key in items.ticket_refs.

One adapter failing never stops the others: a ClickUp outage must not prevent
the Jira ticket from being filed, least of all during a live demo.
"""

from __future__ import annotations

import logging

import config
from adapters.clickup_adapter import ClickUpAdapter
from adapters.jira_adapter import JiraAdapter
from adapters.ticket_base import TicketAdapter, TicketRef

log = logging.getLogger(__name__)

# Adding a platform is one entry here plus one file.
REGISTRY: dict[str, type[TicketAdapter]] = {
    "jira": JiraAdapter,
    "clickup": ClickUpAdapter,
}


class TicketManager:
    def __init__(self, enabled: list[str] | None = None):
        names = enabled if enabled is not None else config.ENABLED_TICKET_ADAPTERS
        self.adapters: list[TicketAdapter] = []
        for name in names:
            adapter_cls = REGISTRY.get(name)
            if adapter_cls is None:
                log.warning(
                    "unknown ticket adapter %r (known: %s) -- skipping",
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

    def create_ticket(self, item: dict) -> dict:
        """File the item on every enabled platform.

        Returns ``{"refs": {...}, "tickets": {...}, "errors": {...}}`` where
        ``refs`` is ready to merge straight into items.ticket_refs.
        """
        refs: dict[str, str] = {}
        tickets: dict[str, TicketRef] = {}
        errors: dict[str, str] = {}

        for adapter in self.adapters:
            try:
                ref = adapter.create_ticket(item)
            except Exception as exc:
                log.error("%s: create_ticket failed: %s", adapter.name, exc)
                errors[adapter.name] = str(exc)
                continue
            tickets[adapter.name] = ref
            refs[adapter.ref_key] = ref.id
            if ref.url:
                refs[f"{adapter.name}_url"] = ref.url

        return {"refs": refs, "tickets": tickets, "errors": errors}

    def update_status(self, ticket_refs: dict, status: str) -> dict:
        results: dict[str, bool] = {}
        errors: dict[str, str] = {}
        for adapter, ref in self._refs_for(ticket_refs):
            try:
                results[adapter.name] = adapter.update_status(ref, status)
            except Exception as exc:
                log.error("%s: update_status failed: %s", adapter.name, exc)
                errors[adapter.name] = str(exc)
        return {"results": results, "errors": errors}

    def add_comment(self, ticket_refs: dict, text: str) -> dict:
        results: dict[str, bool] = {}
        errors: dict[str, str] = {}
        for adapter, ref in self._refs_for(ticket_refs):
            try:
                results[adapter.name] = adapter.add_comment(ref, text)
            except Exception as exc:
                log.error("%s: add_comment failed: %s", adapter.name, exc)
                errors[adapter.name] = str(exc)
        return {"results": results, "errors": errors}

    def _refs_for(self, ticket_refs: dict):
        """Rebuild a TicketRef per adapter from what storage kept."""
        ticket_refs = ticket_refs or {}
        for adapter in self.adapters:
            ticket_id = ticket_refs.get(adapter.ref_key)
            if not ticket_id:
                continue
            yield adapter, TicketRef(
                platform=adapter.name,
                id=str(ticket_id),
                url=ticket_refs.get(f"{adapter.name}_url"),
            )
