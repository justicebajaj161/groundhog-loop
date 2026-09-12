"""LLM-backed structured extraction.

Three prompts live here:
  * extract_action_items   -- transcript  -> list of action items
  * classify_reply         -- free text   -> a status + a note
  * adjudicate_recurrence  -- a shortlist -> which candidates are the same problem

Both go through llm_client.chat_json, so they follow whatever LLM_MODEL is
configured with no provider-specific code.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import llm_client
from storage import Status

log = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Ask for an *object* with an array inside it, never a bare top-level array:
# provider JSON mode rejects a top-level array on several backends, and that
# breaks only after someone swaps LLM_MODEL.
_EXTRACT_SYSTEM = """You extract follow-up action items from engineering meeting \
transcripts (retros, incident post-mortems, standups).

Return ONLY a JSON object of this exact shape:
{"action_items": [{"item": "...", "owner": "...", "due_date_guess": "YYYY-MM-DD", \
"source_context": "...", "recurrence_signal": "..."}]}

Rules:
- "item": the concrete action, phrased as an imperative. One action per entry.
  Describe the underlying work, not the speaker's phrasing.
- "owner": the person named as responsible, or null if nobody was named.
  Use the bare first name as it appears in the transcript.
- "due_date_guess": resolve relative dates ("by Friday", "next sprint") against
  TODAY'S DATE given below, and emit strict YYYY-MM-DD. Use null if no deadline
  was discussed. Never invent a deadline that was not implied.
- "source_context": the short verbatim quote from the transcript that this item
  came from, so a human can audit the extraction. Max 200 characters.
- "recurrence_signal": THE MOST IMPORTANT FIELD FOR THIS SYSTEM. If anywhere in the
  transcript someone indicates this problem has come up before -- "again", "the third
  time", "the same pipeline", "we keep coming back to this", "last time it was X",
  "didn't we talk about this?", "still", "since June" -- copy it VERBATIM here, even
  when it was said by a different speaker and several lines away from the action item
  itself. This is the team telling you the problem recurred, and it is routinely the
  ONLY evidence that two differently-worded items are the same problem.
  Copy EVERY such sentence, not merely the first or the most quotable one, joined with
  a space, and never drop a clause from the middle of one. The clauses that carry the
  most weight are the ones naming WHERE it came up before ("August retro, sprint 42,
  now this") and WHAT was tried before ("last time it was the alerting, before that it
  was the timeout window"). Those name the earlier occurrences, so they are usually the
  only text tying two differently-worded items together -- an item that loses them stops
  being recognisable as a repeat at all. Keep them even if you must drop vaguer wording
  to fit.
  Use "" when nothing in the transcript suggests the issue is a repeat. Max 400 chars.
- Only genuine commitments to future work. Ignore discussion, status updates on
  already-finished work, and vague sentiment.
- If the transcript contains no action items, return {"action_items": []}."""

_CLASSIFY_SYSTEM = """You classify a person's reply about an outstanding action item.

Return ONLY a JSON object of this exact shape:
{"status": "done" | "in_progress" | "blocked", "note": "..."}

Rules:
- "done": the work is finished.
- "in_progress": actively being worked on, or will be shortly, with nothing
  stopping it.
- "blocked": something external is preventing progress -- waiting on a person,
  a dependency, an approval, an outage.
- "note": if blocked, state what the blocker is. If in_progress, any useful
  detail such as an ETA. If done, a short confirmation or "". Keep under 200
  characters, in the replier's own terms.
- When the reply is ambiguous, prefer "in_progress"."""


_ADJUDICATE_SYSTEM = """You decide whether a new engineering action item is a RECURRENCE of \
problems the team already has open -- the same underlying problem surfacing again, not merely \
a similar-sounding task.

You are given one NEW ITEM and a numbered shortlist of CANDIDATES. Judge each candidate \
independently.

Return ONLY a JSON object of this exact shape:
{"matches": [{"id": <candidate id>, "same_problem": true|false, \
"confidence": "high"|"medium"|"low", "recurrence_hint": "..."}]}

Include an entry for EVERY candidate id you were given. Rules:

- "same_problem": true only when both items are driven by the SAME underlying root cause or
  the same unresolved system weakness. The surface task will usually differ -- adding an
  alert, widening a timeout, and writing a design doc can all be symptoms of one unfixed
  problem, and that IS a recurrence.
- Say false when items merely share a technology, a category of work, an owner, or a word.
  Two different testing tasks are not a recurrence. Two different documentation updates are
  not a recurrence. Two unrelated dashboards are not a recurrence.
- The transcript quotes are the strongest evidence you have, and the "the team flagged
  this as a repeat" line most of all. When someone says "again", "third time", "same
  pipeline", "we keep coming back to this", "last time it was X" -- that is the team
  telling you directly that it recurred. Weight it above surface wording: two items
  whose actions sound completely different ARE the same problem when the team says the
  underlying system is the one they keep returning to.
- WHOSE repeat flag it is decides how much it is worth. The NEW ITEM's own "flagged this
  as a repeat" line is the strongest evidence available: if the new item's own words say
  the team keeps coming back to this system, then a candidate on that same system is what
  it came back from -- link it, even when the two actions sound nothing alike.
  A CANDIDATE's repeat flag is much weaker on its own. It proves THAT candidate came back;
  it does not make the new item the thing that came back. Never carry a candidate's flag
  across to a new item working on a different system or root cause: "the ETL keeps failing
  silently" plus "scope a test-data seeding tool" is two problems, however emphatic the
  quote.
- Conversely, an explicit disclaimer ("this isn't related to the incident") is evidence
  about THAT link only; it does not rule out a link to a different candidate.
- "recurrence_hint": when same_problem is true this MUST contain a VERBATIM quote,
  in single quotes, copied word-for-word from one of the "said in the room" lines you
  were given -- plus which meeting it came from. Do NOT paraphrase and do NOT describe
  the systems in your own words. If neither item's quoted context contains wording that
  justifies the link, say so plainly instead of inventing support.
  Prefer quoting the "flagged this as a repeat" line when one is present.
  Good: "same pipeline, per sprint 42 retro -- 'That sync is the same pipeline I've been
  fighting with'"
  Bad:  "both involve the warehouse batch pipeline and its failure modes"
  Keep it under 200 characters. Use "" when same_problem is false.
- Prefer false when genuinely unsure. A missed recurrence is a nuisance; a false one tells
  the team they have a systemic problem they do not have."""


def _normalize_date(value) -> str | None:
    """Accept only a strict ISO date. Anything else becomes None.

    Free-text dates must never reach the database -- the scheduler compares
    due_date as a string, so "next Friday" would sort unpredictably and
    silently never fire a nudge.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "tbd", "unknown"}:
        return None
    if _ISO_DATE_RE.match(text):
        try:
            date.fromisoformat(text)
            return text
        except ValueError:
            return None
    # A full timestamp is fine, we just want the date part.
    if len(text) >= 10 and _ISO_DATE_RE.match(text[:10]):
        try:
            date.fromisoformat(text[:10])
            return text[:10]
        except ValueError:
            return None
    log.debug("dropping unparseable due_date_guess %r", text)
    return None


def _clean(value, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "unassigned", "n/a"}:
        return None
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def extract_action_items(raw_text: str) -> list[dict]:
    """Pull action items out of a transcript.

    Returns a list of ``{item, owner, due_date_guess, source_context}``.
    Entries without an ``item`` are dropped; other fields coerce to None.
    """
    if not (raw_text or "").strip():
        return []

    today = date.today().isoformat()
    user_prompt = f"TODAY'S DATE: {today}\n\nTRANSCRIPT:\n\"\"\"\n{raw_text.strip()}\n\"\"\""

    payload = llm_client.chat_json(_EXTRACT_SYSTEM, user_prompt)

    # Tolerate the handful of keys a model might pick instead of the one we asked for.
    raw_items = None
    for key in ("action_items", "items", "actionItems", "results", "data"):
        if isinstance(payload.get(key), list):
            raw_items = payload[key]
            break
    if raw_items is None:
        log.warning("no action_items array in response; keys were %s", list(payload))
        return []

    items: list[dict] = []
    for entry in raw_items:
        if isinstance(entry, str):
            entry = {"item": entry}
        if not isinstance(entry, dict):
            continue
        item_text = _clean(entry.get("item") or entry.get("action") or entry.get("task"))
        if not item_text:
            continue
        items.append(
            {
                "item": item_text,
                "owner": _clean(entry.get("owner") or entry.get("assignee")),
                "due_date_guess": _normalize_date(
                    entry.get("due_date_guess") or entry.get("due_date")
                ),
                "source_context": _clean(entry.get("source_context") or entry.get("context"), 200),
                # Captured at extraction time because the dialogue that proves a
                # recurrence sits several lines away from the action item and is
                # gone by the time anything downstream looks at it.
                # 400, not 200: the proof of a repeat is often spread over two
                # sentences ("...third time in six weeks. August retro, sprint 42,
                # now this."), and a 200-char cap truncated exactly the clause that
                # names the earlier meetings. Observed 2026-09-12 -- the run that
                # lost it dropped the whole recurrence group and escalated nothing.
                "recurrence_signal": _clean(
                    entry.get("recurrence_signal") or entry.get("recurrence_hint"), 400
                ),
            }
        )
    return items


def adjudicate_recurrence(new_item: dict, candidates: list[dict]) -> dict:
    """Decide which shortlisted candidates are the same underlying problem.

    ``new_item`` and each candidate carry ``item_text``, ``source_context`` and
    ``source_meeting``. Returns ``{candidate_id: {same_problem, confidence,
    recurrence_hint}}`` -- only ids that were actually offered, so a hallucinated
    id cannot invent a link.

    Embeddings shortlist; this decides. Cosine similarity alone was measured
    against real vectors and could not separate the two populations at any
    threshold -- see CLAUDE.md.
    """
    if not candidates:
        return {}

    def describe(entry: dict) -> str:
        out = f'  action: {entry.get("item_text") or entry.get("item")}'
        if entry.get("source_meeting"):
            out += f'\n  meeting: {entry["source_meeting"]}'
        if entry.get("source_context"):
            out += f'\n  said in the room: "{entry["source_context"]}"'
        if entry.get("recurrence_signal"):
            out += f'\n  the team flagged this as a repeat: "{entry["recurrence_signal"]}"'
        return out

    lines = ["NEW ITEM:", describe(new_item), "", "CANDIDATES:"]
    for entry in candidates:
        lines.append(f'[id {entry["id"]}]')
        lines.append(describe(entry))
        lines.append("")

    try:
        payload = llm_client.chat_json(_ADJUDICATE_SYSTEM, "\n".join(lines))
    except llm_client.LLMError:
        # An adjudication failure must not take the transcript down with it.
        # Recall-only would mean trusting a cutoff we know does not work, so the
        # safe failure is "not a recurrence" -- the item is still filed normally.
        log.warning("recurrence adjudication failed; treating item as new", exc_info=True)
        return {}

    offered = {entry["id"] for entry in candidates}
    verdicts: dict[int, dict] = {}
    raw = payload.get("matches")
    if not isinstance(raw, list):
        log.warning("adjudicator returned no matches array; keys were %s", list(payload))
        return {}

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            candidate_id = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        if candidate_id not in offered:
            log.warning("adjudicator invented candidate id %s, ignoring", candidate_id)
            continue
        verdicts[candidate_id] = {
            "same_problem": bool(entry.get("same_problem")),
            "confidence": str(entry.get("confidence") or "").strip().lower() or "unknown",
            "recurrence_hint": _clean(entry.get("recurrence_hint"), 200) or "",
        }
    return verdicts


def classify_reply(reply_text: str) -> dict:
    """Turn a free-text reply into ``{status, note}``.

    Only for genuinely free-form replies. Button presses bypass this entirely
    via pipeline.apply_structured_reply -- that path must never cost an LLM call.
    """
    text = (reply_text or "").strip()
    if not text:
        return {"status": Status.IN_PROGRESS.value, "note": ""}

    payload = llm_client.chat_json(_CLASSIFY_SYSTEM, f"REPLY:\n\"\"\"\n{text}\n\"\"\"")

    raw_status = str(payload.get("status", "")).strip().lower().replace("-", "_").replace(" ", "_")
    if raw_status not in {s.value for s in Status} or raw_status == Status.PENDING.value:
        # Unrecognised status: keep the item moving rather than guessing "done",
        # and preserve the reply so a human can see what was actually said.
        log.warning("unexpected status %r from classifier, defaulting to in_progress", raw_status)
        return {"status": Status.IN_PROGRESS.value, "note": _clean(text, 200) or ""}

    return {"status": raw_status, "note": _clean(payload.get("note"), 200) or ""}
