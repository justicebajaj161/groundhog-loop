"""Central configuration.

Every provider name, credential, endpoint and threshold in the system is
resolved here and nowhere else. No other module reads ``os.environ``.

Precedence:  built-in defaults  <  environment / .env  <  agent_config.json

Only the small "runtime-tunable" subset may be overridden by agent_config.json,
which is what the ``/groundhogloop set ...`` slash command writes to.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

RUNTIME_CONFIG_PATH = Path(os.getenv("AGENT_CONFIG_PATH") or (ROOT / "agent_config.json"))

# Keys that may be changed at runtime without a restart or a redeploy.
RUNTIME_KEYS = frozenset(
    {
        "nudge_days_before",
        "escalate_after_days",
        "recurrence_recall_threshold",
        "recurrence_escalate_count",
        "recurrence_use_llm",
    }
)

_runtime: dict = {}


def _load_runtime() -> dict:
    try:
        with open(RUNTIME_CONFIG_PATH) as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in RUNTIME_KEYS}


# --------------------------------------------------------------------------
# typed getters -- runtime override first, then env, then default
# --------------------------------------------------------------------------


def _raw(key: str):
    """Runtime file wins over the environment for the tunable subset."""
    lowered = key.lower()
    if lowered in _runtime:
        return _runtime[lowered]
    return os.getenv(key)


def _str(key: str, default: str = "") -> str:
    value = _raw(key)
    return default if value is None else str(value)


# Literal placeholders copied out of .env.example. These must count as ABSENT,
# not present: DRY_RUN=unset decides per adapter via is_configured(), so a
# half-filled .env would otherwise flip adapters LIVE and fire real API calls
# with junk credentials. Observed 2026-09-12 -- SLACK_BOT_TOKEN=xoxb-... made
# the Slack adapter go live and attempt 10 real chat.postMessage calls.
# Failing to dry-run is the only unsafe direction here, so this errs that way.
_PLACEHOLDER_SUBSTRINGS = ("your-domain", "example.com", "changeme", "yourteam")


def _is_placeholder(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    # "xoxb-...", "pk_...", "xapp-..." -- the .env.example house style
    if lowered.endswith("..."):
        return True
    # "<your-token-here>"
    if lowered.startswith("<") and lowered.endswith(">"):
        return True
    return any(marker in lowered for marker in _PLACEHOLDER_SUBSTRINGS)


def _opt(key: str) -> str | None:
    """A credential-shaped value: empty string or a placeholder means absent."""
    value = _raw(key)
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if _is_placeholder(value):
        log.warning(
            "%s looks like a placeholder from .env.example (%r) -- treating it as "
            "unset so the adapter dry-runs instead of calling out with a junk credential",
            key,
            value[:24],
        )
        return None
    return value


def _int(key: str, default: int) -> int:
    value = _raw(key)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(key: str, default: float) -> float:
    value = _raw(key)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool) -> bool:
    value = _raw(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _tristate(key: str) -> bool | None:
    """None means 'decide automatically', which is how DRY_RUN defaults."""
    value = _raw(key)
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _list(key: str, default: list[str]) -> list[str]:
    """Accepts both ``jira,clickup`` and ``["jira","clickup"]``."""
    value = _raw(key)
    if value is None or str(value).strip() == "":
        return list(default)
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value if str(v).strip()]
    text = str(value).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip().lower() for v in parsed if str(v).strip()]
        except json.JSONDecodeError:
            pass
    return [part.strip().lower() for part in text.split(",") if part.strip()]


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

# -- LLM / embeddings (one provider, swapped by model id alone) -------------
OPENROUTER_API_KEY: str | None
OPENROUTER_BASE_URL: str
LLM_MODEL: str
EMBEDDING_MODEL: str

# -- everything else is assigned in reload() so the module can be re-read ---


def reload() -> None:
    """(Re)read agent_config.json and refresh every module-level setting."""
    global _runtime
    _runtime = _load_runtime()

    g = globals()

    # LLM layer -- provider is OpenRouter; the *model* is the only swap point.
    g["OPENROUTER_API_KEY"] = _opt("OPENROUTER_API_KEY")
    g["OPENROUTER_BASE_URL"] = _str("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    g["LLM_MODEL"] = _str("LLM_MODEL", "deepseek/deepseek-v3.2")
    # 0.0, not 0.1: extraction must reproduce run-to-run for a scripted demo.
    # Note this reduces variance, it does not guarantee determinism -- providers
    # batch and route non-deterministically regardless of temperature.
    g["LLM_TEMPERATURE"] = _float("LLM_TEMPERATURE", 0.0)
    g["LLM_MAX_RETRIES"] = _int("LLM_MAX_RETRIES", 2)
    g["LLM_JSON_MODE"] = _bool("LLM_JSON_MODE", True)
    # Retries were immediate, so three attempts landed inside one second and a
    # transient upstream 429 failed all of them. Observed 2026-09-12: the free
    # deepseek pool rate-limited mid-run and an adjudication was lost, silently
    # dropping a real recurrence link. Backoff is what makes the retry mean
    # anything; 429 waits longer because the pool needs time to drain.
    g["LLM_RETRY_BASE_DELAY"] = _float("LLM_RETRY_BASE_DELAY", 1.5)
    g["LLM_RETRY_429_DELAY"] = _float("LLM_RETRY_429_DELAY", 5.0)

    g["EMBEDDING_MODEL"] = _str("EMBEDDING_MODEL", "openai/text-embedding-3-small")
    # text-embedding-3-small has an 8192-token context, so the old 1400-char cap
    # (sized for the free model's 512 tokens) was throwing away context the
    # recall stage could use. 8000 chars is ~2000 tokens -- well inside it.
    g["EMBEDDING_MAX_CHARS"] = _int("EMBEDDING_MAX_CHARS", 8000)
    g["EMBEDDING_DIMENSIONS"] = _int("EMBEDDING_DIMENSIONS", 0)  # 0 = model default

    # Route only to provider endpoints that actually implement the parameters we
    # send. deepseek/deepseek-chat-v3.1 has 8 endpoints and one (SambaNova) has
    # no response_format; without this OpenRouter may land there and we lose
    # structured output to the fallback for no reason. Fallback ACROSS providers
    # stays on -- that breadth is the entire point of the slug.
    g["OPENROUTER_REQUIRE_PARAMETERS"] = _bool("OPENROUTER_REQUIRE_PARAMETERS", True)

    # Optional OpenRouter attribution headers.
    g["OPENROUTER_REFERER"] = _str("OPENROUTER_REFERER", "https://localhost/retro-agent")
    g["OPENROUTER_TITLE"] = _str("OPENROUTER_TITLE", "Groundhog Loop")

    # -- recurrence --------------------------------------------------------
    # Two-stage. Embeddings are a cheap RECALL pass that shortlists candidates;
    # the LLM makes the actual same-problem-or-not call on that shortlist.
    #
    # The old single-threshold design is gone. Measured 2026-09-12 against real
    # embeddings, no cutoff existed that grouped the intended thread without
    # also collapsing unrelated items -- the score bands overlap. See CLAUDE.md.
    # So this cutoff is deliberately LOOSE: it only has to avoid missing a real
    # match, because precision is the adjudicator's job.
    # 0.15, not 0.30. Recall must sit BELOW the weakest true pair or the
    # adjudicator cannot fix what recall already threw away -- a 0.30 cutoff
    # silently dropped a real link under the old embedder.
    # Re-measured 2026-09-12 for openai/text-embedding-3-small (1536-d), whose
    # scores run higher than the old free model's: weakest TRUE cross-meeting
    # pair 0.2425 (#4 <-> #8), strongest UNRELATED 0.4121 (#6 <-> #9). The bands
    # OVERLAP on this model too, which is the second independent confirmation
    # that no single cutoff works. 0.15 keeps ~0.09 of headroom under the
    # weakest true pair, deliberately, because extraction wording drifts run to
    # run and drags these scores with it.
    # RECURRENCE_MAX_CANDIDATES bounds the cost of being this generous.
    g["RECURRENCE_RECALL_THRESHOLD"] = _float("RECURRENCE_RECALL_THRESHOLD", 0.15)
    # Cap on how many shortlisted candidates go to the LLM, so a large database
    # cannot turn one transcript into an unbounded number of adjudication calls.
    g["RECURRENCE_MAX_CANDIDATES"] = _int("RECURRENCE_MAX_CANDIDATES", 8)
    # Off = recall only, which is the old (broken) behaviour. Kept as an escape
    # hatch for offline runs, not as a supported mode.
    g["RECURRENCE_USE_LLM"] = _bool("RECURRENCE_USE_LLM", True)
    # A recurrence means the problem came BACK -- it resurfaced in a later
    # meeting. Two action items from one retro about one system are two tasks,
    # not a recurrence, and they are by far the strongest false-positive source
    # (measured: #7<->#8 cosine 0.594, the highest pair in the whole sample).
    g["RECURRENCE_CROSS_MEETING_ONLY"] = _bool("RECURRENCE_CROSS_MEETING_ONLY", True)
    g["RECURRENCE_ESCALATE_COUNT"] = _int("RECURRENCE_ESCALATE_COUNT", 3)

    # Superseded by RECURRENCE_RECALL_THRESHOLD. Read only so a stale .env can
    # be detected and warned about rather than silently ignored.
    g["_LEGACY_RECURRENCE_THRESHOLD"] = _raw("RECURRENCE_THRESHOLD")

    # -- scheduler ---------------------------------------------------------
    g["NUDGE_DAYS_BEFORE"] = _int("NUDGE_DAYS_BEFORE", 2)
    g["ESCALATE_AFTER_DAYS"] = _int("ESCALATE_AFTER_DAYS", 3)
    g["RENUDGE_AFTER_HOURS"] = _int("RENUDGE_AFTER_HOURS", 24)
    g["SCHEDULER_INTERVAL_MINUTES"] = _int("SCHEDULER_INTERVAL_MINUTES", 60)

    # -- storage -----------------------------------------------------------
    g["DB_PATH"] = _str("DB_PATH", str(ROOT / "items.db"))

    # -- adapter enablement ------------------------------------------------
    g["ENABLED_TICKET_ADAPTERS"] = _list("ENABLED_TICKET_ADAPTERS", ["jira", "clickup"])
    g["ENABLED_NOTIFIER_ADAPTERS"] = _list("ENABLED_NOTIFIER_ADAPTERS", ["slack", "teams"])

    # Tri-state: True = never touch the network, False = always call out,
    # None = per-adapter automatic (dry-run exactly when creds are missing).
    g["DRY_RUN"] = _tristate("DRY_RUN")

    # -- Jira --------------------------------------------------------------
    _jira_url = (_str("JIRA_BASE_URL", "")).rstrip("/")
    g["JIRA_BASE_URL"] = "" if _is_placeholder(_jira_url) else _jira_url
    g["JIRA_EMAIL"] = _opt("JIRA_EMAIL")
    g["JIRA_API_TOKEN"] = _opt("JIRA_API_TOKEN")
    g["JIRA_PROJECT_KEY"] = _str("JIRA_PROJECT_KEY", "OPS")
    g["JIRA_ISSUE_TYPE"] = _str("JIRA_ISSUE_TYPE", "Task")
    # Our status vocabulary -> the transition *name* in your Jira workflow.
    g["JIRA_STATUS_MAP"] = _json_map(
        "JIRA_STATUS_MAP",
        {"pending": "To Do", "in_progress": "In Progress", "blocked": "Blocked", "done": "Done"},
    )

    # -- ClickUp -----------------------------------------------------------
    g["CLICKUP_API_TOKEN"] = _opt("CLICKUP_API_TOKEN")
    g["CLICKUP_LIST_ID"] = _opt("CLICKUP_LIST_ID")
    g["CLICKUP_BASE_URL"] = _str("CLICKUP_BASE_URL", "https://api.clickup.com/api/v2").rstrip("/")
    g["CLICKUP_STATUS_MAP"] = _json_map(
        "CLICKUP_STATUS_MAP",
        {"pending": "to do", "in_progress": "in progress", "blocked": "blocked", "done": "complete"},
    )

    # -- Slack -------------------------------------------------------------
    g["SLACK_BOT_TOKEN"] = _opt("SLACK_BOT_TOKEN")
    g["SLACK_APP_TOKEN"] = _opt("SLACK_APP_TOKEN")  # xapp-... , Socket Mode
    g["SLACK_SIGNING_SECRET"] = _opt("SLACK_SIGNING_SECRET")
    g["SLACK_TEAM_CHANNEL"] = _str("SLACK_TEAM_CHANNEL", "#eng-retro")
    # Transcripts name owners as first names ("Dana"); DMs need a real
    # member ID. Map them here: {"dana": "U01ABCDEF"}.
    g["SLACK_USER_MAP"] = _json_map("SLACK_USER_MAP", {})

    # -- Teams (Power Automate Workflows webhooks) -------------------------
    g["TEAMS_CHANNEL_WEBHOOK_URL"] = _opt("TEAMS_CHANNEL_WEBHOOK_URL")
    g["TEAMS_DM_WEBHOOK_URL"] = _opt("TEAMS_DM_WEBHOOK_URL")
    g["TEAMS_TEAM_CHANNEL"] = _str("TEAMS_TEAM_CHANNEL", "Engineering / Retro")
    g["TEAMS_USER_MAP"] = _json_map("TEAMS_USER_MAP", {})

    # -- misc --------------------------------------------------------------
    g["HTTP_TIMEOUT"] = _int("HTTP_TIMEOUT", 30)


def _json_map(key: str, default: dict[str, str]) -> dict[str, str]:
    value = _raw(key)
    if value is None or str(value).strip() == "":
        return dict(default)
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return dict(default)
    if not isinstance(parsed, dict):
        return dict(default)
    merged = dict(default)
    merged.update({str(k): str(v) for k, v in parsed.items()})
    return merged


def set_runtime(key: str, value) -> dict:
    """Persist one runtime-tunable value to agent_config.json and re-read.

    Used by the Slack/Teams slash command handler.
    """
    key = key.strip().lower()
    if key not in RUNTIME_KEYS:
        raise KeyError(f"{key!r} is not runtime-tunable. Options: {sorted(RUNTIME_KEYS)}")

    try:
        with open(RUNTIME_CONFIG_PATH) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}

    data[key] = value
    with open(RUNTIME_CONFIG_PATH, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")

    reload()
    return data


def describe() -> dict:
    """Non-secret snapshot of the active configuration, for the demo banner."""
    return {
        "llm_model": LLM_MODEL,
        "embedding_model": EMBEDDING_MODEL,
        "base_url": OPENROUTER_BASE_URL,
        "openrouter_key": "set" if OPENROUTER_API_KEY else "MISSING",
        "ticket_adapters": ENABLED_TICKET_ADAPTERS,
        "notifier_adapters": ENABLED_NOTIFIER_ADAPTERS,
        "dry_run": "auto" if DRY_RUN is None else DRY_RUN,
        "recurrence_recall_threshold": RECURRENCE_RECALL_THRESHOLD,
        "recurrence_use_llm": RECURRENCE_USE_LLM,
        "recurrence_max_candidates": RECURRENCE_MAX_CANDIDATES,
        "recurrence_escalate_count": RECURRENCE_ESCALATE_COUNT,
        "nudge_days_before": NUDGE_DAYS_BEFORE,
        "escalate_after_days": ESCALATE_AFTER_DAYS,
        "db_path": DB_PATH,
    }


reload()
