"""Two-stage recurrence detection: embedding recall, then LLM adjudication.

The point of the whole agent: an action item that reads as new but is actually
the third time the team has agreed to fix the same thing.

This was originally a single cosine threshold. That was measured against real
embeddings on 2026-09-12 and it does not work. The intended thread scored
0.18-0.34 while unrelated pairs reached 0.59 -- the bands overlap, so NO cutoff
separates them, and a sweep of every threshold confirmed it. The reason is not a
weak model: "make the ETL alert on failure", "widen the staging sync window" and
"checkpoint the warehouse batch" are three genuinely different actions on three
different subsystems. They are the same *problem* only because the team said so,
out loud, in the transcript -- "this is the third time we've had an action item
about this pipeline".

So the two stages have different jobs:

  1. RECALL (here)  -- cosine over embeddings at a deliberately loose cutoff.
     Cheap, and only has to avoid missing a real match. Precision is not its job.
  2. ADJUDICATION   -- the LLM reads both items and the words actually spoken in
     the room, and decides same-problem-or-not. See extraction.adjudicate_recurrence.

Embeddings narrow the field; the model makes the call.
"""

from __future__ import annotations

import logging
import uuid

import numpy as np

import config
import llm_client

log = logging.getLogger(__name__)


def cosine_similarity(a, b) -> float:
    """Cosine similarity of two vectors, in [-1, 1]. 0.0 if either is zero."""
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    if va.shape != vb.shape or va.size == 0:
        return 0.0
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def new_group_id() -> str:
    return f"rg-{uuid.uuid4().hex[:10]}"


def shortlist_candidates(
    query: list[float],
    stored_embeddings: list[dict],
    *,
    threshold: float | None = None,
    limit: int | None = None,
    exclude_meeting: str | None = None,
) -> tuple[list[dict], list[tuple[int, float]], int]:
    """Stage 1: cheap cosine recall. Returns (candidates, all_scores, skipped).

    Deliberately loose. Anything plausible goes to the adjudicator; nothing is
    decided here.
    """
    cutoff = config.RECURRENCE_RECALL_THRESHOLD if threshold is None else threshold
    cap = config.RECURRENCE_MAX_CANDIDATES if limit is None else limit
    query_dim = len(query)

    scores: list[tuple[int, float]] = []
    candidates: list[dict] = []
    skipped_dims = 0

    for record in stored_embeddings:
        stored = record.get("embedding")
        if not stored:
            continue
        # Vectors from a different embedding model are not comparable. Without
        # this guard, changing EMBEDDING_MODEL silently produces garbage
        # similarities rather than an error anyone would notice.
        if len(stored) != query_dim:
            skipped_dims += 1
            continue
        score = cosine_similarity(query, stored)
        scores.append((record["id"], score))
        if score <= cutoff:
            continue
        # Same-meeting items are two tasks from one discussion, not a problem
        # coming back. This is the dominant false-positive source.
        if (
            exclude_meeting
            and config.RECURRENCE_CROSS_MEETING_ONLY
            and record.get("source_meeting") == exclude_meeting
        ):
            continue
        entry = dict(record)
        entry["score"] = score
        candidates.append(entry)

    scores.sort(key=lambda pair: pair[1], reverse=True)
    candidates.sort(key=lambda rec: rec["score"], reverse=True)
    return candidates[:cap], scores, skipped_dims


def check_recurrence(
    item_text: str,
    stored_embeddings: list[dict],
    *,
    vector: list[float] | None = None,
    threshold: float | None = None,
    item: dict | None = None,
    adjudicator=None,
) -> dict:
    """Decide whether ``item_text`` is a repeat of something already tracked.

    ``stored_embeddings`` is a list of records as returned by
    ``storage.all_embeddings()``: dicts with at least ``id``, ``embedding``,
    ``source_context``, ``source_meeting`` and ``recurrence_group_id``.

    Pass ``vector`` to skip the embedding call. Pass ``item`` to give the
    adjudicator the new item's own context and meeting. Pass ``adjudicator`` to
    substitute the LLM call (``--fake-llm``, tests).

    Returns, when a match is confirmed::

        {"is_recurring": True, "linked_ids": [4, 11], "count": 3,
         "group_id": "rg-...", "best_score": 0.34, "matched_text": "...",
         "recurrence_hint": "same pipeline, sprint 42 retro -- '...'",
         "vector": [...], "scores": [...], "candidates": [...], "verdicts": {...}}

    and ``{"is_recurring": False, ...}`` otherwise.
    """
    query = vector if vector is not None else llm_client.embed(item_text)
    meta = dict(item or {})
    meta.setdefault("item_text", item_text)

    # -- stage 1: recall ---------------------------------------------------
    candidates, scores, skipped_dims = shortlist_candidates(
        query,
        stored_embeddings,
        threshold=threshold,
        exclude_meeting=meta.get("source_meeting"),
    )

    if skipped_dims:
        log.warning(
            "skipped %s stored item(s) whose embedding dimension != %s -- these were "
            "embedded with a different model and cannot be compared",
            skipped_dims,
            len(query),
        )

    best_score = scores[0][1] if scores else 0.0
    base = {
        "vector": query,
        "scores": scores[:5],
        "compared": len(scores),
        "skipped_dim_mismatch": skipped_dims,
        "candidates": [
            {"id": c["id"], "score": round(c["score"], 4), "item_text": c.get("item_text")}
            for c in candidates
        ],
        "recall_threshold": (
            config.RECURRENCE_RECALL_THRESHOLD if threshold is None else threshold
        ),
    }
    not_recurring = {
        **base,
        "is_recurring": False,
        "linked_ids": [],
        "count": 1,
        "group_id": None,
        "best_score": round(best_score, 4),
        "matched_text": None,
        "recurrence_hint": None,
        "verdicts": {},
    }

    if not candidates:
        return not_recurring

    # -- stage 2: adjudication --------------------------------------------
    if adjudicator is None:
        if not config.RECURRENCE_USE_LLM:
            # Recall-only. Retained as an escape hatch; known not to separate.
            log.warning("RECURRENCE_USE_LLM is off -- trusting the recall cutoff alone")
            adjudicator = lambda new, cands: {  # noqa: E731
                c["id"]: {"same_problem": True, "confidence": "unknown", "recurrence_hint": ""}
                for c in cands
            }
        else:
            from extraction import adjudicate_recurrence

            adjudicator = adjudicate_recurrence

    verdicts = adjudicator(meta, candidates) or {}
    base["verdicts"] = verdicts

    confirmed = [c for c in candidates if verdicts.get(c["id"], {}).get("same_problem")]
    if not confirmed:
        return {**not_recurring, "verdicts": verdicts}

    confirmed.sort(key=lambda rec: rec["score"], reverse=True)
    best_record = confirmed[0]

    # Adopt an existing group where there is one, or open a new one if none of
    # the confirmed candidates has ever been grouped (each was a first
    # occurrence, recorded before any repeat existed).
    #
    # A new occurrence can be the evidence that two SEPARATELY tracked threads
    # are one problem, so every group represented among the confirmed candidates
    # has to be merged -- not just the best-scoring one's. Taking only
    # best_record's group silently orphaned items: observed 2026-09-12, #4 was
    # judged unrelated to #3, #7 then linked to #4 and opened group A over
    # {4,7}, and #8 linked to both #3 and #4 -- whose best match was the
    # ungrouped #3, so a fresh group B was opened and {3,4} re-stamped into it.
    # #7 was left pointing at A alone, out of a thread it had been correctly
    # linked into, and the occurrence count read 3 instead of 4.
    existing_groups = {
        record["recurrence_group_id"]
        for record in confirmed
        if record.get("recurrence_group_id")
    }
    group_id = (
        best_record.get("recurrence_group_id")
        # Deterministic pick so the same shortlist always merges the same way.
        or (sorted(existing_groups)[0] if existing_groups else new_group_id())
    )

    # Everything confirmed, plus every member of every group being merged in.
    merged_groups = existing_groups | {group_id}
    linked = {record["id"] for record in confirmed}
    linked.update(
        record["id"]
        for record in stored_embeddings
        if record.get("recurrence_group_id") in merged_groups
    )

    hint = next(
        (
            verdicts[c["id"]]["recurrence_hint"]
            for c in confirmed
            if verdicts.get(c["id"], {}).get("recurrence_hint")
        ),
        None,
    )

    return {
        **base,
        "is_recurring": True,
        "linked_ids": sorted(linked),
        "count": len(linked) + 1,  # +1 for the occurrence being checked now
        "group_id": group_id,
        "best_score": round(best_record["score"], 4),
        "matched_text": best_record.get("item_text"),
        "matched_id": best_record["id"],
        "recurrence_hint": hint,
    }
