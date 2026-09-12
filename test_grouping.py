#!/usr/bin/env python3
"""Regression test for the recurrence grouping -- the one thing the demo depends on.

Every recurrence decision in this system is the LLM adjudicator's. There is no
numeric backstop: the cosine stage only does recall, and a wrong adjudication is
a wrong answer that logs nothing. On 2026-09-12 the same config produced
{3,4,7,8,9}, {3,4,7,8} and {3,8} on three consecutive runs -- the last with zero
escalations, which would have silently killed the demo. Two bugs that each
destroyed the group (group merging, and truncated recurrence evidence) were
found by reading logs, not by any test. This is that test.

What it pins, against the three sample transcripts:

  * the ETL-alerting / staging-sync / checkpointing / completion-marker thread
    forms ONE group,
  * the test-data seeding item and the on-call dashboard item stay out of it,
  * the thread reaches occurrence 4,
  * exactly 2 channel escalations fire.

It runs the REAL models on purpose. Model drift is precisely what it exists to
catch, so --fake-llm would defeat it. It costs roughly one demo run.

    .venv/bin/python test_grouping.py                        # one run, exit 0/1
    .venv/bin/python test_grouping.py --runs 3
    .venv/bin/python test_grouping.py --model deepseek/deepseek-v3.2 --runs 3
    .venv/bin/python test_grouping.py --check-db items.db    # audit a real run, no LLM

Side effects: none. DRY_RUN and DB_PATH are forced before config is imported, so
no Jira issue is created, nothing reaches #eng-retro, and the project's own
items.db is never opened.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Isolation. This block MUST run before `import config`: config.reload() runs at
# import time and reads os.environ, and AdapterBase.dry_run reads config.DRY_RUN.
# Getting this wrong means a test run files real tickets.
# --------------------------------------------------------------------------
_TMP_DIR = tempfile.mkdtemp(prefix="groundhog-regression-")
os.environ["DRY_RUN"] = "true"
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "items.db")

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def green(t):   return _c("32", t)
def red(t):     return _c("1;31", t)
def yellow(t):  return _c("33", t)
def bold(t):    return _c("1", t)
def dim(t):     return _c("2", t)


# --------------------------------------------------------------------------
# Which item is which.
#
# NOT by id. Ids are SQLite autoincrement and only come out as 1..9 when
# extraction yields exactly 3/3/3 per transcript, which is not guaranteed --
# extraction is not bit-reproducible even at temperature 0, and one model swap
# invented a tenth action item. So each expected item is found by a predicate
# over its text, and an ambiguous or missing match is itself a failure rather
# than something that quietly passes.
# --------------------------------------------------------------------------

def _p_etl(t):        return "etl" in t and ("alert" in t or "silent" in t)
def _p_staging(t):    return "staging" in t and "sync" in t
def _p_checkpoint(t): return "checkpoint" in t or ("design doc" in t and ("batch" in t or "warehouse" in t))


def _p_marker(t):
    # Wording drifts hard on this one: "completion marker", "completion signal",
    # "warehouse batch load", "warehouse load". Match the concept, not a phrase.
    if not ("marker" in t or ("completion" in t and "signal" in t)):
        return False
    return "warehouse" in t or "batch" in t or "load" in t
def _p_seeding(t):    return "seed" in t and "test" in t


def _p_dashboard(t):
    # "dashboard" alone is not enough: #2 is "update the checkout runbook to
    # point to the current Grafana dashboard", and #6 is the on-call handover
    # doc. Both would collide with a naive match.
    if "dashboard" not in t or "runbook" in t or "handover" in t:
        return False
    return "on-call" in t or "on call" in t or "warehouse" in t


# label -> (predicate, expected_in_group, description)
EXPECTED = {
    "etl_alert":         (_p_etl,        True,  "make the nightly ETL failure alert someone"),
    "staging_sync":      (_p_staging,    True,  "investigate the staging sync timeouts"),
    "checkpoint":        (_p_checkpoint, True,  "design doc for checkpointing the warehouse batch"),
    "completion_marker": (_p_marker,     True,  "completion marker for the warehouse batch"),
    "seeding":           (_p_seeding,    False, "test-data seeding tool  (must stay out)"),
    "oncall_dashboard":  (_p_dashboard,  False, "warehouse batch on the on-call dashboard  (must stay out)"),
}

EXPECTED_ITEMS = 9
EXPECTED_COUNT = 4
EXPECTED_ESCALATIONS = 2


class Check:
    """One assertion line, so the report reads the same whether it passed."""

    def __init__(self, label: str, value: str, ok: bool, note: str = ""):
        self.label, self.value, self.ok, self.note = label, value, ok, note

    def render(self) -> str:
        mark = green("OK") if self.ok else red("FAIL")
        line = f"  {self.label:<18}: {self.value:<34} {mark}"
        if self.note:
            line += f"\n                      {dim(self.note)}"
        return line


def _resolve(items: list[dict]) -> tuple[dict, list[Check]]:
    """Map each label to exactly one item. Ambiguity is a failure, not a guess."""
    found, checks = {}, []
    for label, (pred, _want, desc) in EXPECTED.items():
        hits = [it for it in items if pred((it.get("item_text") or "").lower())]
        if len(hits) == 1:
            found[label] = hits[0]
        else:
            checks.append(Check(
                f"identify {label}", f"{len(hits)} matches (expected 1)", False,
                f"looking for: {desc}"
                + (("  |  matched: " + " / ".join(
                    (h.get("item_text") or "")[:48] for h in hits)) if hits else ""),
            ))
    if checks:
        # A predicate that misses is usually the predicate's fault, not the
        # model's -- extraction rewords items run to run. Print everything so
        # the difference is visible rather than guessed at.
        checks[-1].note += "\n                      extracted this run:\n" + "\n".join(
            f"                        #{it['id']} {(it.get('item_text') or '')[:90]}"
            for it in items
        )
    return found, checks


def _grade(items: list[dict], escalations: int | None) -> tuple[list[Check], dict]:
    """Apply every assertion to a list of item dicts with recurrence columns."""
    checks: list[Check] = []
    diag: dict = {}

    checks.append(Check(
        "items extracted", str(len(items)), len(items) == EXPECTED_ITEMS,
        "" if len(items) == EXPECTED_ITEMS else
        "extraction is not bit-reproducible; a count other than 9 usually means "
        "the model split or merged an action item",
    ))

    found, id_checks = _resolve(items)
    checks.extend(id_checks)
    if id_checks:
        # Without a reliable mapping the rest of the report would be fiction.
        return checks, diag

    wanted_in = [lbl for lbl, (_p, want, _d) in EXPECTED.items() if want]
    groups = {found[lbl].get("recurrence_group_id") for lbl in wanted_in}
    member_ids = sorted(found[lbl]["id"] for lbl in wanted_in)
    group_id = next(iter(groups)) if len(groups) == 1 else None

    checks.append(Check(
        "group members",
        " ".join(f"#{i}" for i in member_ids) + (f"  ({group_id})" if group_id else ""),
        len(groups) == 1 and group_id is not None,
        "" if len(groups) == 1 and group_id else
        "the thread did not form as one group -- groups seen: "
        + ", ".join(str(g) for g in sorted(groups, key=str)),
    ))

    for lbl in ("seeding", "oncall_dashboard"):
        item = found[lbl]
        joined = group_id is not None and item.get("recurrence_group_id") == group_id
        num = f"#{item['id']}"
        checks.append(Check(
            f"{num} rejected", "no -- JOINED the group" if joined else "yes", not joined,
            f"{EXPECTED[lbl][2]}" if joined else "",
        ))

    counts = [found[lbl].get("recurrence_count") or 1 for lbl in wanted_in]
    peak = max(counts) if counts else 0
    diag["peak"] = peak
    checks.append(Check(
        "peak count", str(peak), peak == EXPECTED_COUNT,
        "" if peak == EXPECTED_COUNT else
        f"expected occurrence #{EXPECTED_COUNT}; a lower count means a link never formed",
    ))

    if escalations is not None:
        checks.append(Check(
            "escalations", str(escalations), escalations == EXPECTED_ESCALATIONS,
            "" if escalations == EXPECTED_ESCALATIONS else
            f"expected {EXPECTED_ESCALATIONS} channel posts to the team "
            f"(occurrence >= the escalate threshold)",
        ))

    # The leading indicator for #9. In a correct run the on-call dashboard item
    # carries no repeat flag of its own -- Marcus's "third time ... August retro,
    # sprint 42" line belongs to the checkpointing item. When extraction attaches
    # it here instead, this item is about to be linked.
    sig = found["oncall_dashboard"].get("recurrence_signal")
    diag["dashboard_signal"] = sig
    return checks, diag


# --------------------------------------------------------------------------
# live run
# --------------------------------------------------------------------------


def run_once(db_path: str) -> tuple[list[dict], int, dict]:
    """One full pipeline pass over all three transcripts. Returns (items, escalations, hints)."""
    import storage
    import pipeline
    from adapters import _http
    from adapters.notifier_manager import NotifierManager
    from adapters.ticket_manager import TicketManager

    # CALL_LOG is a module global that nothing resets between runs.
    _http.clear_calls()
    storage.reset_db(db_path)

    escalations = 0
    hints: dict[int, str] = {}

    def on_event(event: str, payload: dict) -> None:
        nonlocal escalations
        if event == "recurrence_alert":
            escalations += 1
        elif event == "stored":
            pass

    tickets, notifiers = TicketManager(), NotifierManager()
    for path in sorted((ROOT / "sample_transcripts").glob("*.txt")):
        name = path.stem.replace("_", " ").title()
        results = pipeline.process_transcript(
            path.read_text(), name,
            ticket_manager=tickets, notifier_manager=notifiers,
            on_event=on_event, db_path=db_path,
        )
        for r in results:
            if r.recurrence.get("recurrence_hint"):
                hints[r.item_id] = r.recurrence["recurrence_hint"]

    # Cross-check the escalation count independently of the event hook. Only
    # recurrence_alert posts without an `item=`, so it is the one Slack call
    # that carries no Block Kit blocks.
    blockless = sum(
        1 for c in _http.CALL_LOG
        if c["adapter"] == "slack" and c["kind"] == "chat_postMessage"
        and (c.get("body") or {}).get("blocks") is None
    )
    if blockless != escalations:
        print(yellow(f"  note: event hook counted {escalations} escalations, "
                     f"Slack CALL_LOG counted {blockless}"))

    return storage.all_items(db_path), escalations, hints


def report(checks: list[Check], diag: dict, hints: dict, verbose: bool) -> bool:
    ok = all(c.ok for c in checks)
    for c in checks:
        print(c.render())
    if ok:
        print(f"  {bold(green('-> PASS'))}")
    else:
        print(f"  {bold(red('-> FAIL'))}   {red('do not record; re-run or switch model')}")

    sig = diag.get("dashboard_signal")
    if sig:
        print(f"  {yellow('#9 diagnostics')}    : recurrence_signal is SET -- "
              f"extraction attached a repeat flag to the dashboard item")
        print(dim(f"                      \"{sig[:150]}\""))
    elif "dashboard_signal" in diag:
        print(dim("  #9 diagnostics    : recurrence_signal=NULL (good -- no repeat flag attached)"))

    if verbose and hints:
        print(dim("  link evidence:"))
        for item_id, hint in sorted(hints.items()):
            print(dim(f"    #{item_id}: \"{hint[:120]}\""))
    return ok


# --------------------------------------------------------------------------
# db audit
# --------------------------------------------------------------------------


def check_db(db_path: str, verbose: bool) -> bool:
    import storage

    items = storage.all_items(db_path)
    if not items:
        print(red(f"  {db_path} has no items -- nothing to audit"))
        return False

    print(bold(f"AUDIT  {db_path}   {len(items)} item(s)"))
    # Escalations are not persisted anywhere, so they cannot be measured here.
    checks, diag = _grade(items, escalations=None)
    ok = report(checks, diag, {}, verbose)
    if "peak" in diag:
        implied = max(0, diag["peak"] - 2)
        print(dim(f"  escalations       : {implied} implied by a group of {diag['peak']} "
                  f"(derived, not measured -- escalations are not stored)"))
    return ok


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pin the recurrence grouping before a demo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--runs", type=int, default=1, help="how many live runs (default 1)")
    ap.add_argument("--model", help="override LLM_MODEL for this invocation")
    ap.add_argument("--check-db", nargs="?", const="", metavar="PATH",
                    help="audit an existing database instead of running; no model calls")
    ap.add_argument("--keep", action="store_true", help="keep the temporary database")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the link evidence quotes")
    args = ap.parse_args()

    if args.check_db is not None:
        os.environ.pop("DB_PATH", None)  # so config.DB_PATH is the project default again
        import config
        config.reload()
        target = args.check_db or config.DB_PATH
        return 0 if check_db(target, args.verbose) else 1

    if args.model:
        os.environ["LLM_MODEL"] = args.model
    import config
    config.reload()

    if not config.OPENROUTER_API_KEY:
        print(red("OPENROUTER_API_KEY is not set -- this test runs the real models by design."))
        return 2

    print(bold("GROUNDHOG LOOP -- grouping regression"))
    print(f"  model      {config.LLM_MODEL}")
    print(f"  embeddings {config.EMBEDDING_MODEL}")
    print(f"  dry-run    {config.DRY_RUN}   db {config.DB_PATH}")
    print(dim(f"  expecting  group of {EXPECTED_COUNT}, {EXPECTED_ESCALATIONS} escalations, "
              f"#5 and #9 rejected"))

    passed = 0
    try:
        for run in range(1, args.runs + 1):
            db = os.path.join(_TMP_DIR, f"run{run}.db")
            started = time.time()
            print()
            try:
                items, escalations, hints = run_once(db)
            except Exception as exc:  # a crashed run is a failed run, not a traceback
                print(bold(f"REGRESSION  run {run}/{args.runs}   "
                           f"model={config.LLM_MODEL}   {time.time() - started:.1f}s"))
                print(f"  {red('-> FAIL')}   the run raised: {type(exc).__name__}: {exc}")
                continue
            print(bold(f"REGRESSION  run {run}/{args.runs}   "
                       f"model={config.LLM_MODEL}   {time.time() - started:.1f}s"))
            checks, diag = _grade(items, escalations)
            if report(checks, diag, hints, args.verbose):
                passed += 1
    finally:
        if args.keep:
            print(dim(f"\n  databases kept in {_TMP_DIR}"))
        else:
            shutil.rmtree(_TMP_DIR, ignore_errors=True)

    print()
    verdict = green("PASS") if passed == args.runs else red("FAIL")
    print(bold(f"PASS RATE: {passed}/{args.runs}  {config.LLM_MODEL}   {verdict}"))
    return 0 if passed == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
