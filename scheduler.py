"""Nudging and escalation.

run_n_cycle() is the whole job. Call it from cron, from the demo CLI, or on a
timer with --loop. It is idempotent within RENUDGE_AFTER_HOURS: re-running it
does not re-DM people, which matters both in production and when someone runs
the demo three times in a row on stage.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta, timezone

import config
import storage
from adapters.notifier_manager import NotifierManager

log = logging.getLogger(__name__)


def _days_between(due: str | None) -> int | None:
    """Days from today until ``due``. Negative means overdue."""
    if not due:
        return None
    try:
        return (date.fromisoformat(due[:10]) - date.today()).days
    except ValueError:
        log.warning("unparseable due_date %r", due)
        return None


def _hours_since(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        then = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


def run_n_cycle(
    *,
    notifier_manager: NotifierManager | None = None,
    db_path: str | None = None,
    force: bool = False,
) -> dict:
    """One pass over everything still open.

    a. every item that is not done
    b. due within nudge_days_before (or already past) -> DM the owner, with
       quick-reply buttons where the platform supports them
    c. overdue by more than escalate_after_days -> post to the team channel,
       not just the owner's DMs

    ``force=True`` ignores the re-nudge guard, for demoing the same items twice.
    """
    # Pick up any change the slash command made since the last cycle.
    config.reload()
    storage.init_db(db_path)

    notifiers = notifier_manager if notifier_manager is not None else NotifierManager()

    summary = {
        "open_items": 0,
        "nudged": 0,
        "nudges_skipped_recent": 0,
        "escalated": 0,
        "escalations_skipped_recent": 0,
        "no_due_date": 0,
        "nudge_days_before": config.NUDGE_DAYS_BEFORE,
        "escalate_after_days": config.ESCALATE_AFTER_DAYS,
    }

    open_items = storage.open_items(db_path)
    summary["open_items"] = len(open_items)
    summary["no_due_date"] = sum(1 for item in open_items if not item.get("due_date"))

    # -- (c) escalate first, so a badly overdue item is not merely DMed ----
    escalated_ids = set()
    for item in storage.items_overdue_by(config.ESCALATE_AFTER_DAYS, db_path):
        days_left = _days_between(item.get("due_date"))
        if days_left is None:
            continue
        hours = _hours_since(item.get("last_escalated_at"))
        if not force and hours is not None and hours < config.RENUDGE_AFTER_HOURS:
            summary["escalations_skipped_recent"] += 1
            escalated_ids.add(item["id"])
            continue

        notifiers.escalate(item, abs(days_left))
        storage.mark_escalated(item["id"], db_path)
        escalated_ids.add(item["id"])
        summary["escalated"] += 1

    # -- (b) nudge owners of everything due soon or already past ----------
    for item in storage.items_due_within(config.NUDGE_DAYS_BEFORE, db_path):
        days_left = _days_between(item.get("due_date"))
        if days_left is None:
            continue
        hours = _hours_since(item.get("last_nudged_at"))
        if not force and hours is not None and hours < config.RENUDGE_AFTER_HOURS:
            summary["nudges_skipped_recent"] += 1
            continue

        notifiers.nudge(item, days_left)
        storage.mark_nudged(item["id"], db_path)
        summary["nudged"] += 1

    log.info("cycle complete: %s", summary)
    return summary


def run_demo_followup_cycle(
    *,
    notifier_manager: NotifierManager | None = None,
    db_path: str | None = None,
) -> dict:
    """DM blocked and in-progress items immediately for a recording.

    This deliberately bypasses the real scheduler's due-date and 24-hour
    guards so the follow-up loop is visible in seconds. It never escalates to
    the team channel and does not update ``last_nudged_at``, leaving the normal
    production schedule untouched.
    """
    config.reload()
    storage.init_db(db_path)
    notifiers = notifier_manager if notifier_manager is not None else NotifierManager()
    eligible = [
        item for item in storage.open_items(db_path)
        if item.get("status") in {"in_progress", "blocked"}
    ]

    for item in eligible:
        notifiers.nudge(item)

    summary = {
        "followed_up": len(eligible),
        "item_ids": [item["id"] for item in eligible],
        "statuses": [item["status"] for item in eligible],
    }
    log.info("demo follow-up cycle complete: %s", summary)
    return summary


# --------------------------------------------------------------------------
# runtime configuration, driven from Slack/Teams
# --------------------------------------------------------------------------

_HELP = (
    "Usage:\n"
    "  /groundhogloop show\n"
    "  /groundhogloop set nudge_days_before 3\n"
    "  /groundhogloop set escalate_after_days 5\n"
    "  /groundhogloop set recurrence_recall_threshold 0.3\n"
    "  /groundhogloop set recurrence_escalate_count 3\n"
    "  /groundhogloop run"
)


def handle_config_command(text: str) -> str:
    """Back the ``/groundhogloop`` slash command. Returns text to reply with.

    Changes are written to agent_config.json, which config.reload() layers over
    the environment, so a tweak during a demo takes effect on the next cycle
    without a restart.
    """
    parts = (text or "").strip().split()
    if not parts or parts[0] in {"help", "-h", "--help"}:
        return _HELP

    verb = parts[0].lower()

    if verb in {"show", "get", "status"}:
        config.reload()
        lines = ["Current settings:"]
        for key in sorted(config.RUNTIME_KEYS):
            lines.append(f"  {key} = {getattr(config, key.upper())}")
        lines.append(f"  (editable in {config.RUNTIME_CONFIG_PATH})")
        return "\n".join(lines)

    if verb == "run":
        summary = run_n_cycle()
        return "Cycle complete: " + ", ".join(f"{k}={v}" for k, v in summary.items())

    if verb == "set":
        if len(parts) < 3:
            return f"Need a key and a value.\n{_HELP}"
        key, raw_value = parts[1].lower(), parts[2]
        try:
            value: float | int = float(raw_value)
            if value.is_integer() and "threshold" not in key:
                value = int(value)
        except ValueError:
            return f"{raw_value!r} is not a number."
        try:
            config.set_runtime(key, value)
        except KeyError as exc:
            return exc.args[0]
        return f"{key} is now {value}."

    return f"Unknown command {verb!r}.\n{_HELP}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run nudge/escalation cycles.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run a single due-date cycle (default)")
    mode.add_argument("--loop", action="store_true", help="run every SCHEDULER_INTERVAL_MINUTES")
    mode.add_argument(
        "--demo-followup",
        action="store_true",
        help="DM blocked/in-progress items repeatedly for a recording; ignores due dates",
    )
    parser.add_argument("--force", action="store_true", help="ignore the re-nudge guard")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=10,
        help="seconds between --demo-followup cycles (default: 10)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.demo_followup:
        if args.interval_seconds <= 0:
            parser.error("--interval-seconds must be greater than zero")
        log.info(
            "demo follow-up mode: blocked/in-progress items every %ss; Ctrl-C to stop",
            args.interval_seconds,
        )
        try:
            while True:
                summary = run_demo_followup_cycle()
                print("  " + ", ".join(f"{key}: {value}" for key, value in summary.items()))
                time.sleep(args.interval_seconds)
        except KeyboardInterrupt:
            log.info("demo follow-up stopped")
        return 0

    if args.loop:
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
        except ImportError:
            print("--loop needs apscheduler: pip install apscheduler")
            return 1

        scheduler = BlockingScheduler()
        scheduler.add_job(
            lambda: run_n_cycle(force=args.force),
            "interval",
            minutes=config.SCHEDULER_INTERVAL_MINUTES,
            next_run_time=datetime.now(),
        )
        log.info("looping every %s minutes; Ctrl-C to stop", config.SCHEDULER_INTERVAL_MINUTES)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("stopped")
        return 0

    summary = run_n_cycle(force=args.force)
    for key, value in summary.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
