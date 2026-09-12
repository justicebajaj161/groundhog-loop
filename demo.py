#!/usr/bin/env python3
"""Traceable CLI for Groundhog Loop.

Runs a transcript through the full pipeline and narrates every step, so the
whole chain is visible in a terminal during a live demo:

    extraction -> recurrence check -> ticket creation (all platforms)
                                   -> notification (all platforms)

    python demo.py --sample all --reset-db      # the scripted demo
    python demo.py --transcript notes.txt       # your own file
    cat notes.txt | python demo.py --stdin
    python demo.py --run-scheduler              # nudges + escalations
    python demo.py --list                       # what's in the database
    python demo.py --reply 3 "blocked on vendor"    # free-text path (LLM)
    python demo.py --button 3 done                  # button path (no LLM)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sys
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent
SAMPLES = sorted((ROOT / "sample_transcripts").glob("*.txt"))

# ---------------------------------------------------------------------------
# terminal formatting
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty() and os.getenv("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def bold(t):    return _c("1", t)
def dim(t):     return _c("2", t)
def cyan(t):    return _c("36", t)
def green(t):   return _c("32", t)
def yellow(t):  return _c("33", t)
def red(t):     return _c("31", t)
def magenta(t): return _c("35", t)

WIDTH = 78
_step = {"n": 0}


def rule(char: str = "-") -> None:
    print(dim(char * WIDTH))


def banner(title: str) -> None:
    print()
    print(bold(cyan("=" * WIDTH)))
    print(bold(cyan(f"  {title}")))
    print(bold(cyan("=" * WIDTH)))


def step(title: str) -> None:
    _step["n"] += 1
    print()
    print(bold(f"[{_step['n']}] {title}"))
    rule()


def kv(key: str, value, indent: int = 2) -> None:
    print(f"{' ' * indent}{dim(key + ':'):<28} {value}")


def wrap(text: str, indent: int = 4) -> str:
    out = []
    for line in text.split("\n"):
        if not line.strip():
            out.append("")
            continue
        while len(line) > WIDTH - indent:
            cut = line.rfind(" ", 0, WIDTH - indent)
            cut = cut if cut > 0 else WIDTH - indent
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return "\n".join(" " * indent + l for l in out)


# ---------------------------------------------------------------------------
# offline stand-in for the model, for --fake-llm
# ---------------------------------------------------------------------------

# Offline stand-in for a real embedding model.
#
# Vectors are a bag of content words, plus a strong signal for a few specific
# multi-word phrases that name the same underlying system. That is enough for
# the recurring thread in the sample transcripts to group while unrelated items
# stay apart -- which makes the offline run look like the live one.
#
# It is still only lexical. A real embedding model matches "the nightly ETL job
# failed" to "warehouse loads are backing up" on meaning alone; this cannot.
_CONCEPT_PHRASES = {
    "data_pipeline": [
        "nightly etl", "etl job", "staging data sync", "warehouse batch pipeline",
        "warehouse load", "data pipeline", "batch window",
    ],
}

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "onto", "add",
    "adding", "make", "making", "get", "got", "can", "our", "its", "it's", "was",
    "are", "has", "have", "not", "but", "all", "any", "out", "off", "per", "via",
    "a", "an", "to", "of", "on", "in", "at", "by", "is", "be", "so", "up", "or",
}

_FAKE_DIM = 64


def _hash_vector(text: str, dim: int = _FAKE_DIM) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    raw = [(digest[i % len(digest)] / 255.0) - 0.5 for i in range(dim)]
    return _normalize(raw)


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _word_vector(text: str, dim: int = _FAKE_DIM) -> list[float]:
    """Bag of content words, hashed into buckets."""
    vector = [0.0] * dim
    for word in re.findall(r"[a-z0-9']+", text.lower()):
        if len(word) < 3 or word in _STOPWORDS:
            continue
        bucket = int(hashlib.sha256(word.encode()).hexdigest(), 16) % dim
        vector[bucket] += 1.0
    if not any(vector):
        return _hash_vector(text, dim)
    return _normalize(vector)


def _fake_embed_batch(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        lowered = (text or "").lower()
        words = _word_vector(lowered)
        concept = None
        for name, phrases in _CONCEPT_PHRASES.items():
            if any(phrase in lowered for phrase in phrases):
                concept = _hash_vector(f"concept::{name}")
                break
        if concept is None:
            vectors.append(words)
            continue
        # Concept dominates so the recurring thread groups; the word component
        # keeps distinct items in that thread from being literally identical.
        vectors.append(_normalize([0.92 * c + 0.08 * w for c, w in zip(concept, words)]))
    return vectors


_FAKE_EXTRACTIONS = {
    "INC-4471": [
        {"item": "Add a regression test asserting the DB connection pool size at startup",
         "owner": "Sam", "due_date_guess": "2026-09-14",
         "source_context": "add a regression test that asserts the connection pool size at startup"},
        {"item": "Update the checkout runbook to point at the current Grafana dashboard",
         "owner": "Priya", "due_date_guess": "2026-09-12",
         "source_context": "our runbook for checkout still points at the old Grafana dashboard"},
        {"item": "Make the nightly ETL job alert on failure instead of exiting zero",
         "owner": "Dana", "due_date_guess": "2026-09-13",
         "source_context": "the nightly ETL job failed silently again on Tuesday"},
    ],
    "Sprint 42 Retro": [
        {"item": "Fix the staging data sync timing out during the deploy window",
         "owner": "Dana", "due_date_guess": "2026-09-20",
         "source_context": "the staging data sync timed out during the deploy window"},
        {"item": "Scope a test-data seeding tool for accounts with known balances",
         "owner": "Jules", "due_date_guess": "2026-09-15",
         "source_context": "we still don't have a way to seed test accounts with known balances"},
        {"item": "Update the on-call handover doc and its escalation contacts",
         "owner": "Priya", "due_date_guess": "2026-09-15",
         "source_context": "the on-call handover doc hasn't been updated since June"},
    ],
    "Finance close delayed": [
        {"item": "Write a design doc for checkpointing the warehouse batch pipeline",
         "owner": "Dana", "due_date_guess": "2026-09-23",
         "source_context": "warehouse loads are backing up overnight"},
        {"item": "Add a completion marker finance can check before pulling numbers",
         "owner": "Dana", "due_date_guess": "2026-09-12",
         "source_context": "can you add a completion marker as a stopgap"},
        {"item": "Add the warehouse batch job to the on-call dashboard",
         "owner": "Priya", "due_date_guess": "2026-09-16",
         "source_context": "add the warehouse batch to the on-call dashboard"},
    ],
}


def _fake_adjudicate(user_prompt: str) -> dict:
    """Offline stand-in for the recurrence adjudicator.

    Uses the same hard-coded concept phrases as the fake embedder, so the
    offline run tells the same story as the live one. It is not reasoning --
    it is a lookup, and it proves plumbing only.
    """
    blocks = re.split(r"\[id (\d+)\]", user_prompt)
    head = blocks[0].lower()
    new_is_pipeline = any(
        phrase in head for phrase in _CONCEPT_PHRASES["data_pipeline"]
    )
    matches = []
    for i in range(1, len(blocks) - 1, 2):
        cid, body = int(blocks[i]), blocks[i + 1].lower()
        same = new_is_pipeline and any(
            phrase in body for phrase in _CONCEPT_PHRASES["data_pipeline"]
        )
        matches.append({
            "id": cid,
            "same_problem": same,
            "confidence": "high" if same else "low",
            "recurrence_hint": (
                "same data pipeline discussed in an earlier meeting (stubbed adjudicator)"
                if same else ""
            ),
        })
    return {"matches": matches}


def _fake_chat_json(system_prompt: str, user_prompt: str, **kwargs) -> dict:
    if "same_problem" in system_prompt:
        return _fake_adjudicate(user_prompt)
    if "classify" in system_prompt.lower() or "replier" in system_prompt.lower():
        lowered = user_prompt.lower()
        if any(w in lowered for w in ("done", "finished", "shipped", "merged")):
            return {"status": "done", "note": ""}
        if any(w in lowered for w in ("blocked", "waiting", "stuck", "can't", "cannot")):
            return {"status": "blocked", "note": "reported a blocker (stubbed classifier)"}
        return {"status": "in_progress", "note": "stubbed classifier"}

    for marker, items in _FAKE_EXTRACTIONS.items():
        if marker in user_prompt:
            return {"action_items": items}

    # Unknown transcript: crude heuristic so arbitrary input still flows.
    found = []
    for line in user_prompt.split("\n"):
        stripped = line.strip()
        if len(stripped) > 25 and any(
            cue in stripped.lower() for cue in ("action item", "can you", "i'll take", "take that")
        ):
            found.append(
                {"item": stripped[:160], "owner": None, "due_date_guess": None,
                 "source_context": stripped[:120]}
            )
    return {"action_items": found[:5]}


def install_fake_llm() -> None:
    import llm_client

    llm_client.chat_json = _fake_chat_json
    llm_client.embed_batch = _fake_embed_batch
    llm_client.embed = lambda text: _fake_embed_batch([text])[0]
    print(yellow(
        "  [FAKE-LLM] Model calls are stubbed. Extraction is canned and embeddings are\n"
        "             deterministic concept vectors. This exercises the plumbing only --\n"
        "             it does NOT demonstrate real semantic matching. Drop --fake-llm\n"
        "             with an OPENROUTER_API_KEY set to test that for real."
    ))


# ---------------------------------------------------------------------------
# trace rendering
# ---------------------------------------------------------------------------


def make_printer(show_payloads: bool):
    from adapters import _http

    seen = {"calls": 0}

    def flush_calls(label: str) -> None:
        calls = _http.recent_calls(seen["calls"])
        seen["calls"] = _http.call_count()
        for call in calls:
            tag = yellow("[DRY-RUN]") if call["dry_run"] else green("[LIVE]")
            print(f"    {tag} {bold(call['adapter']):<18} {call['method']:<5} {call['url']}")
            if call.get("error"):
                print(f"      {red('error:')} {call['error'][:200]}")
            if show_payloads and call.get("body"):
                print(dim(wrap(json.dumps(call["body"], indent=2, default=str)[:1800], 6)))

    def on_event(event: str, payload: dict) -> None:
        if event == "extract_start":
            step(f"EXTRACTION — {payload['meeting']} ({payload['chars']} chars)")
            kv("model", f"{config.LLM_MODEL} via {config.OPENROUTER_BASE_URL}")

        elif event == "extract_done":
            items = payload["items"]
            kv("items found", len(items))
            print()
            for i, entry in enumerate(items, 1):
                print(f"  {bold(str(i) + '.')} {entry['item']}")
                print(f"     {dim('owner')} {entry.get('owner') or '—':<12} "
                      f"{dim('due')} {entry.get('due_date_guess') or '—'}")
                if entry.get("source_context"):
                    print(dim(f'     "{entry["source_context"]}"'))
                if entry.get("recurrence_signal"):
                    print(magenta(f'     ⟲ team flagged a repeat: "{entry["recurrence_signal"]}"'))

        elif event == "embed_done":
            step(f"EMBEDDING — {payload['count']} item(s), {payload['dim']} dimensions")
            kv("model", config.EMBEDDING_MODEL)

        elif event == "recurrence":
            result = payload["result"]
            print()
            print(f"  {bold('?')} {payload['item']}")
            # stage 1 -- cheap cosine recall
            kv("stage 1: recall", f"{result['compared']} stored item(s) compared, "
                                  f"cutoff {result['recall_threshold']}", 4)
            if result["scores"]:
                top = ", ".join(f"#{i}={s:.3f}" for i, s in result["scores"][:3])
                kv("top similarities", top, 4)
            cands = result.get("candidates") or []
            if not cands:
                kv("shortlisted", dim("none — below recall cutoff, no LLM call"), 4)
            else:
                kv("shortlisted", ", ".join(f"#{c['id']}@{c['score']:.3f}" for c in cands), 4)
                # stage 2 -- the LLM decides
                kv("stage 2: adjudicator", f"{config.LLM_MODEL} judged {len(cands)} candidate(s)", 4)
                for cid, verdict in sorted((result.get("verdicts") or {}).items()):
                    same = verdict.get("same_problem")
                    mark = magenta("SAME PROBLEM") if same else dim("not the same")
                    print(f"      #{cid}: {mark} ({verdict.get('confidence', '?')})")
                    if verdict.get("recurrence_hint"):
                        print(dim(wrap(f'"{verdict["recurrence_hint"]}"', 10)))
            if result["is_recurring"]:
                print(f"    {bold(magenta('-> RECURRING'))} "
                      f"occurrence #{result['count']}, group {result['group_id']}")
                kv("matched", f'"{result["matched_text"]}" @ {result["best_score"]}', 4)
                kv("linked ids", result["linked_ids"], 4)
                if result.get("recurrence_hint"):
                    kv("why", magenta(f'"{result["recurrence_hint"]}"'), 4)
            else:
                why = "no candidate survived adjudication" if cands else "nothing shortlisted"
                print(f"    {green('-> new')} ({why}; best cosine {result['best_score']})")

        elif event == "stored":
            kv("stored as item", f"#{payload['item_id']}", 4)

        elif event == "tickets":
            step(f"TICKETS — item #{payload['item_id']} fanned out to all enabled platforms")
            flush_calls("tickets")
            if payload["refs"]:
                kv("refs stored", json.dumps(payload["refs"]))
            for name, error in (payload.get("errors") or {}).items():
                print(f"  {red('FAILED')} {name}: {error[:200]}")
                print(dim("         (other platforms were unaffected)"))

        elif event == "notified":
            step(f"NOTIFY — item #{payload['item_id']}")
            flush_calls("notify")
            for delivery in payload["deliveries"]:
                mark = green("ok") if delivery.ok else red("FAILED")
                print(f"    {bold(delivery.platform):<18} -> {delivery.target} [{mark}] "
                      f"{dim(delivery.detail or '')}")

        elif event == "recurrence_alert":
            step(bold(magenta(
                f"RECURRENCE ESCALATION — occurrence #{payload['count']} "
                f">= threshold of {config.RECURRENCE_ESCALATE_COUNT}"
            )))
            flush_calls("alert")
            for delivery in payload["deliveries"]:
                print(f"    {bold(delivery.platform):<18} -> {delivery.target} "
                      f"[{green('ok') if delivery.ok else red('FAILED')}]")

    return on_event, flush_calls


def print_config(tickets, notifiers) -> None:
    banner("GROUNDHOG LOOP")
    info = config.describe()
    kv("LLM", info["llm_model"])
    kv("Embeddings", info["embedding_model"])
    kv("Provider", info["base_url"])
    key_state = green("set") if info["openrouter_key"] == "set" else red("MISSING")
    kv("OPENROUTER_API_KEY", key_state)
    kv("Ticket adapters", ", ".join(tickets.describe()) or red("none enabled"))
    kv("Notifier adapters", ", ".join(notifiers.describe()) or red("none enabled"))
    kv("Recurrence", f"recall cutoff {info['recurrence_recall_threshold']} -> "
                     f"{'LLM adjudication' if info['recurrence_use_llm'] else red('recall only')}, "
                     f"escalate at {info['recurrence_escalate_count']} occurrences")
    kv("Scheduler", f"nudge {info['nudge_days_before']}d before, "
                    f"escalate {info['escalate_after_days']}d after")
    kv("Database", info["db_path"])


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_list() -> None:
    import storage

    storage.init_db()
    items = storage.all_items()
    banner(f"DATABASE — {len(items)} item(s)")
    if not items:
        print(dim("  (empty)"))
        return
    for item in items:
        flag = magenta(f" [recurring x{item['recurrence_count']}]") if item["recurrence_count"] > 1 else ""
        print(f"\n  {bold('#' + str(item['id']))} {item['item_text']}{flag}")
        kv("owner / due", f"{item['owner'] or '—'} / {item['due_date'] or '—'}", 4)
        kv("status", item["status"] + (f" — {item['blocker_note']}" if item["blocker_note"] else ""), 4)
        kv("meeting", item["source_meeting"] or "—", 4)
        if item["ticket_refs"]:
            kv("tickets", json.dumps(item["ticket_refs"]), 4)
        if item["recurrence_group_id"]:
            kv("group", item["recurrence_group_id"], 4)


def cmd_run_scheduler(show_payloads: bool) -> None:
    import scheduler

    banner("SCHEDULER CYCLE")
    _, flush = make_printer(show_payloads)
    summary = scheduler.run_n_cycle()
    step("Outbound calls this cycle")
    flush("scheduler")
    step("Summary")
    for key, value in summary.items():
        kv(key, value)


def cmd_reply(item_id: int, text: str, show_payloads: bool) -> None:
    import pipeline

    banner(f"FREE-TEXT REPLY — item #{item_id}")
    print(f"  reply: {bold(text)}")
    _, flush = make_printer(show_payloads)
    step("Classifying with the LLM")
    kv("model", config.LLM_MODEL)
    result = pipeline.apply_text_reply(item_id, text)
    if result is None:
        print(red(f"  no item #{item_id}"))
        return
    kv("status", bold(result["status"]))
    kv("note", result.get("blocker_note") or "—")
    step("Mirrored to tickets")
    flush("reply")


def cmd_button(item_id: int, status: str, show_payloads: bool) -> None:
    import pipeline
    from adapters import _http

    banner(f"BUTTON REPLY — item #{item_id} -> {status}")
    print(yellow("  This path sets the status directly and never calls the LLM."))
    _, flush = make_printer(show_payloads)
    before = _http.call_count()
    result = pipeline.apply_structured_reply(item_id, status)
    if result is None:
        print(red(f"  no item #{item_id}"))
        return
    step("Result")
    kv("status", bold(result["status"]))
    kv("LLM calls made", green("0 — structured path bypasses the model"))
    step("Mirrored to tickets")
    flush("button")


def cmd_process(paths: list[Path], meeting: str | None, show_payloads: bool) -> None:
    import pipeline
    from adapters.notifier_manager import NotifierManager
    from adapters.ticket_manager import TicketManager

    tickets, notifiers = TicketManager(), NotifierManager()
    print_config(tickets, notifiers)

    on_event, _ = make_printer(show_payloads)
    grand_total = 0
    recurring_total = 0

    for path in paths:
        raw = path.read_text()
        name = meeting or path.stem.replace("_", " ").title()
        banner(f"TRANSCRIPT: {path.name}")
        print(dim(wrap(raw[:400] + ("\n..." if len(raw) > 400 else ""))))

        results = pipeline.process_transcript(
            raw, name, ticket_manager=tickets, notifier_manager=notifiers, on_event=on_event
        )
        grand_total += len(results)
        recurring_total += sum(1 for r in results if r.recurrence["is_recurring"])

    banner("RUN COMPLETE")
    kv("Items processed", grand_total)
    kv("Flagged recurring", magenta(str(recurring_total)) if recurring_total else "0")
    print()
    print(dim("  Next:  python demo.py --list"))
    print(dim("         python demo.py --run-scheduler"))
    print(dim("         python demo.py --button <id> done        (no LLM)"))
    print(dim("         python demo.py --reply  <id> 'blocked on vendor'"))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Groundhog Loop — traceable demo pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--transcript", type=Path, help="path to a transcript file")
    source.add_argument("--stdin", action="store_true", help="read a transcript from stdin")
    source.add_argument("--sample", help="1, 2, 3 or 'all' — bundled sample transcripts")
    source.add_argument("--run-scheduler", action="store_true", help="run one nudge/escalate cycle")
    source.add_argument("--list", action="store_true", help="show what's in the database")
    source.add_argument("--reply", nargs=2, metavar=("ID", "TEXT"), help="free-text reply (uses LLM)")
    source.add_argument("--button", nargs=2, metavar=("ID", "STATUS"), help="button reply (no LLM)")

    parser.add_argument("--meeting", help="meeting name to record against these items")
    parser.add_argument("--reset-db", action="store_true", help="wipe the database first")
    parser.add_argument("--fake-llm", action="store_true", help="stub model calls, run offline")
    parser.add_argument("--payloads", action="store_true", help="print full request bodies")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format=dim("  %(levelname)s %(name)s: %(message)s"),
    )

    if args.fake_llm:
        install_fake_llm()

    if args.reset_db:
        import storage

        storage.reset_db()
        print(dim(f"  database reset: {config.DB_PATH}"))

    if args.list:
        cmd_list()
        return 0
    if args.run_scheduler:
        cmd_run_scheduler(args.payloads)
        return 0
    if args.reply:
        cmd_reply(int(args.reply[0]), args.reply[1], args.payloads)
        return 0
    if args.button:
        cmd_button(int(args.button[0]), args.button[1], args.payloads)
        return 0

    if args.stdin:
        text = sys.stdin.read()
        tmp = ROOT / ".stdin_transcript.txt"
        tmp.write_text(text)
        paths = [tmp]
    elif args.transcript:
        if not args.transcript.exists():
            print(red(f"no such file: {args.transcript}"))
            return 1
        paths = [args.transcript]
    elif args.sample:
        if args.sample.lower() == "all":
            paths = SAMPLES
        else:
            try:
                paths = [SAMPLES[int(args.sample) - 1]]
            except (ValueError, IndexError):
                print(red(f"--sample must be 'all' or 1..{len(SAMPLES)}"))
                return 1
    else:
        parser.print_help()
        return 0

    if not paths:
        print(red("no transcripts found"))
        return 1

    cmd_process(paths, args.meeting, args.payloads)
    return 0


if __name__ == "__main__":
    sys.exit(main())
