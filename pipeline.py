"""End-to-end orchestration.

Lives apart from demo.py because the scheduler and the Slack listener need the
same paths -- importing them from a CLI script would be backwards.

    transcript -> extract -> embed -> recurrence -> store -> tickets -> notify

Status updates arrive by two routes that must stay separate:
  * apply_structured_reply -- a button press. Sets status directly. No LLM.
  * apply_text_reply       -- free text. Classified by the LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import config
import extraction
import llm_client
import recurrence as recurrence_mod
import storage
from adapters.notifier_base import NotifierAdapter
from adapters.notifier_manager import NotifierManager
from adapters.ticket_manager import TicketManager
from storage import Status

log = logging.getLogger(__name__)


@dataclass
class ItemResult:
    """Everything that happened to one extracted action item."""

    item_id: int
    extracted: dict
    recurrence: dict
    ticket_refs: dict = field(default_factory=dict)
    ticket_errors: dict = field(default_factory=dict)
    deliveries: list = field(default_factory=list)
    recurrence_alerted: bool = False


def _noop(event: str, payload: dict) -> None:
    pass


def process_transcript(
    raw_text: str,
    meeting_name: str,
    *,
    ticket_manager: TicketManager | None = None,
    notifier_manager: NotifierManager | None = None,
    on_event=None,
    db_path: str | None = None,
    notify: bool = True,
) -> list[ItemResult]:
    """Run a transcript through the whole pipeline.

    ``on_event(name, payload)`` is called at each step so a caller can render
    a live trace without this module knowing anything about printing.
    """
    emit = on_event or _noop
    tickets = ticket_manager if ticket_manager is not None else TicketManager()
    notifiers = notifier_manager if notifier_manager is not None else NotifierManager()

    storage.init_db(db_path)

    emit("extract_start", {"meeting": meeting_name, "chars": len(raw_text)})
    extracted = extraction.extract_action_items(raw_text)
    emit("extract_done", {"items": extracted})

    if not extracted:
        return []

    # One embedding request for the whole batch rather than one per item.
    texts = [entry["item"] for entry in extracted]
    vectors = llm_client.embed_batch(texts)
    emit("embed_done", {"count": len(vectors), "dim": len(vectors[0]) if vectors else 0})

    results: list[ItemResult] = []
    for entry, vector in zip(extracted, vectors):
        results.append(
            _process_one(
                entry,
                vector,
                meeting_name,
                tickets,
                notifiers,
                emit,
                db_path,
                notify,
            )
        )
    return results


def _process_one(
    entry: dict,
    vector: list[float],
    meeting_name: str,
    tickets: TicketManager,
    notifiers: NotifierManager,
    emit,
    db_path: str | None,
    notify: bool,
) -> ItemResult:
    item_text = entry["item"]

    # -- recurrence ------------------------------------------------------
    stored = storage.all_embeddings(db_path)
    # The adjudicator needs the words actually spoken in the room -- that is
    # where "third time we've had an action item about this pipeline" lives,
    # and it is the evidence cosine similarity cannot see.
    rec = recurrence_mod.check_recurrence(
        item_text,
        stored,
        vector=vector,
        item={
            "item_text": item_text,
            "source_context": entry.get("source_context"),
            "recurrence_signal": entry.get("recurrence_signal"),
            "source_meeting": meeting_name,
        },
    )
    emit("recurrence", {"item": item_text, "result": rec})

    # Ticket ids of the earlier occurrences, so the new ticket can point back.
    prior_refs: list[str] = []
    if rec["is_recurring"]:
        by_id = {record["id"]: record for record in stored}
        for linked_id in rec["linked_ids"]:
            for key, value in (by_id.get(linked_id, {}).get("ticket_refs") or {}).items():
                if key.endswith("_id") and value:
                    prior_refs.append(f"{key[:-3]}:{value}")
    rec["prior_refs"] = prior_refs

    # -- persist ---------------------------------------------------------
    item_id = storage.insert_item(
        item_text=item_text,
        owner=entry.get("owner"),
        source_meeting=meeting_name,
        due_date=entry.get("due_date_guess"),
        status=Status.PENDING,
        embedding=vector,
        embedding_model=config.EMBEDDING_MODEL,
        recurrence_group_id=rec["group_id"],
        recurrence_count=rec["count"],
        source_context=entry.get("source_context"),
        recurrence_hint=rec.get("recurrence_hint"),
        recurrence_signal=entry.get("recurrence_signal"),
        db_path=db_path,
    )

    if rec["is_recurring"]:
        # The first occurrence predates the group, so stamp it backwards.
        storage.assign_group(rec["linked_ids"], rec["group_id"], db_path)
        storage.bump_group_count(rec["group_id"], rec["count"], db_path)

    emit("stored", {"item_id": item_id, "group": rec["group_id"], "count": rec["count"]})

    # -- tickets ---------------------------------------------------------
    item = storage.get_item(item_id, db_path) or {}
    item["recurrence"] = rec
    item["source_context"] = entry.get("source_context")

    ticket_out = tickets.create_ticket(item)
    if ticket_out["refs"]:
        storage.set_ticket_refs(item_id, ticket_out["refs"], db_path)
    emit("tickets", {"item_id": item_id, **ticket_out})

    # Point the earlier tickets at the new one, so someone opening the
    # original sees that it came back rather than only the other way round.
    if rec["is_recurring"] and ticket_out["refs"]:
        new_ids = ", ".join(
            f"{k[:-3]}:{v}" for k, v in ticket_out["refs"].items() if k.endswith("_id")
        )
        for linked_id in rec["linked_ids"]:
            linked = storage.get_item(linked_id, db_path)
            if linked and linked.get("ticket_refs"):
                tickets.add_comment(
                    linked["ticket_refs"],
                    f"This issue has recurred (occurrence #{rec['count']}). "
                    f"Newly filed as {new_ids}. Group {rec['group_id']}."
                    + (f"\nWhy this was linked: {rec['recurrence_hint']}"
                       if rec.get("recurrence_hint") else ""),
                )

    # -- notify ----------------------------------------------------------
    item = storage.get_item(item_id, db_path) or {}
    item["recurrence"] = rec
    deliveries = []
    alerted = False

    if notify:
        text = NotifierAdapter.build_assignment(item)
        owner = item.get("owner")
        deliveries = (
            notifiers.dm_user(owner, text, item=item)
            if owner
            else notifiers.post_message(None, f"(unassigned)\n{text}", item=item)
        )
        emit("notified", {"item_id": item_id, "deliveries": deliveries})

        # The whole point: once something has come back enough times, stop
        # quietly filing tickets and say so where the team will see it.
        if rec["is_recurring"] and rec["count"] >= config.RECURRENCE_ESCALATE_COUNT:
            alert = notifiers.recurrence_alert(item, rec)
            alerted = True
            emit("recurrence_alert", {"item_id": item_id, "deliveries": alert, "count": rec["count"]})

    return ItemResult(
        item_id=item_id,
        extracted=entry,
        recurrence=rec,
        ticket_refs=ticket_out["refs"],
        ticket_errors=ticket_out["errors"],
        deliveries=deliveries,
        recurrence_alerted=alerted,
    )


# --------------------------------------------------------------------------
# status updates
# --------------------------------------------------------------------------


def apply_structured_reply(
    item_id: int,
    status: str,
    *,
    note: str | None = None,
    ticket_manager: TicketManager | None = None,
    db_path: str | None = None,
) -> dict | None:
    """Button-press path. Sets status directly -- never calls the LLM.

    Requirement 7: structured replies must bypass the model entirely, so this
    function deliberately imports nothing from extraction.
    """
    resolved = Status.coerce(status)
    item = storage.update_status(item_id, resolved, note, db_path=db_path)
    if item is None:
        log.warning("structured reply for unknown item %s", item_id)
        return None

    tickets = ticket_manager if ticket_manager is not None else TicketManager()
    if item.get("ticket_refs"):
        tickets.update_status(item["ticket_refs"], resolved.value)
        if note:
            tickets.add_comment(item["ticket_refs"], f"Owner update: {note}")
    return item


def apply_text_reply(
    item_id: int,
    reply_text: str,
    *,
    ticket_manager: TicketManager | None = None,
    db_path: str | None = None,
) -> dict | None:
    """Free-text path. Classifies with the LLM, then mirrors to the tickets."""
    classified = extraction.classify_reply(reply_text)
    item = storage.update_status(
        item_id, classified["status"], classified.get("note") or None, db_path=db_path
    )
    if item is None:
        log.warning("text reply for unknown item %s", item_id)
        return None

    tickets = ticket_manager if ticket_manager is not None else TicketManager()
    if item.get("ticket_refs"):
        tickets.update_status(item["ticket_refs"], classified["status"])
        comment = f'Owner replied: "{reply_text.strip()}"'
        if classified.get("note"):
            comment += f"\nClassified as {classified['status']}: {classified['note']}"
        tickets.add_comment(item["ticket_refs"], comment)

    result = dict(item)
    result["classification"] = classified
    return result
