"""Slack backend.

Sending uses the Web API client that Bolt exposes as ``app.client``; the
receiving half (button clicks, free-text replies, the slash command) lives in
slack_listener.py, which runs a Bolt app over Socket Mode. Pass that app's
client in via ``SlackAdapter(client=app.client)`` so the live process shares
one connection.

The three quick-reply buttons carry the item id in ``value`` and the status in
``action_id``; the handler reads them and writes the status with no LLM call.
"""

from __future__ import annotations

import itertools
import logging

import config
from adapters.notifier_base import STATUS_ACTIONS, Delivery, NotifierAdapter

log = logging.getLogger(__name__)

_fake_ts = itertools.count(1)


def status_blocks(item: dict, text: str) -> list[dict]:
    """Block Kit rendering: the message plus three quick-reply buttons."""
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
    item_id = item.get("id")
    if item_id is not None:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"status_actions_{item_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": f"status_{status}",
                        "text": {"type": "plain_text", "text": label},
                        "value": str(item_id),
                        **({"style": "primary"} if status == "done" else {}),
                        **({"style": "danger"} if status == "blocked" else {}),
                    }
                    for status, label in STATUS_ACTIONS
                ],
            }
        )
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Or just reply in your own words -- item "
                        f"#{item_id} will be classified automatically.",
                    }
                ],
            }
        )
    return blocks


class SlackAdapter(NotifierAdapter):
    name = "slack"
    supports_buttons = True

    def __init__(self, client=None):
        self._client = client

    def missing_config(self) -> list[str]:
        missing = []
        if not config.SLACK_BOT_TOKEN:
            missing.append("SLACK_BOT_TOKEN")
        if not _sdk_available():
            missing.append("slack_sdk package")
        return missing

    def default_channel(self) -> str:
        return config.SLACK_TEAM_CHANNEL

    @property
    def client(self):
        if self._client is None:
            from slack_sdk import WebClient  # imported late: dry-run needs no SDK

            self._client = WebClient(token=config.SLACK_BOT_TOKEN)
        return self._client

    def _dry_run_response(self, method: str, url: str, body: dict | None, kind: str) -> dict:
        if kind == "conversations_open":
            return {"ok": True, "channel": {"id": f"D0{next(_fake_ts):05d}"}}
        return {"ok": True, "ts": f"17{next(_fake_ts):09d}.000100", "channel": url}

    # -- user resolution --------------------------------------------------

    def resolve_user(self, user: str) -> str | None:
        """Turn whatever the transcript called someone into a Slack target.

        Transcripts say "Dana"; the API needs U01ABCDEF. Accepts a member ID
        directly, an email (looked up), or a name present in SLACK_USER_MAP.
        """
        if not user:
            return None
        candidate = user.strip().lstrip("@")

        if candidate[:1] in {"U", "W"} and candidate.isupper() and len(candidate) >= 9:
            return candidate

        mapped = config.SLACK_USER_MAP.get(candidate.lower())
        if mapped:
            return mapped

        if "@" in candidate:
            if self.dry_run:
                return f"U{abs(hash(candidate)) % 10**8:08d}"
            try:
                found = self.client.users_lookupByEmail(email=candidate)
                return found["user"]["id"]
            except Exception as exc:
                log.warning("slack: email lookup failed for %s: %s", candidate, exc)
                return None

        return None

    # -- contract ---------------------------------------------------------

    def post_message(self, channel: str, text: str, *, item: dict | None = None) -> Delivery:
        target = channel or self.default_channel()
        blocks = status_blocks(item, text) if item else None
        payload = {"channel": target, "text": _plain(text), "blocks": blocks}

        response = self._sdk_call(
            "chat_postMessage",
            target,
            payload,
            lambda: self.client.chat_postMessage(
                channel=target, text=_plain(text), blocks=blocks
            ),
        )
        return Delivery(self.name, target, ok=True, detail=f"ts={response.get('ts')}")

    def dm_user(self, user: str, text: str, *, item: dict | None = None) -> Delivery:
        user_id = self.resolve_user(user)
        if not user_id:
            # Not a failure worth losing the nudge over: fall back to the team
            # channel so the item still gets in front of a human.
            log.warning(
                "slack: cannot resolve %r to a member ID -- add it to SLACK_USER_MAP. "
                "Falling back to %s",
                user,
                self.default_channel(),
            )
            fallback = self.post_message(
                self.default_channel(), f"(couldn't DM {user})\n{text}", item=item
            )
            fallback.detail = f"fallback to channel; {fallback.detail}"
            return fallback

        opened = self._sdk_call(
            "conversations_open",
            user_id,
            {"users": [user_id]},
            lambda: self.client.conversations_open(users=[user_id]),
        )
        channel_id = (opened.get("channel") or {}).get("id") or user_id

        blocks = status_blocks(item, text) if item else None
        self._sdk_call(
            "chat_postMessage",
            channel_id,
            {"channel": channel_id, "text": _plain(text), "blocks": blocks},
            lambda: self.client.chat_postMessage(
                channel=channel_id, text=_plain(text), blocks=blocks
            ),
        )
        return Delivery(self.name, f"{user} ({user_id})", ok=True, detail="dm")


def _sdk_available() -> bool:
    try:
        import slack_sdk  # noqa: F401

        return True
    except ImportError:
        return False


def _plain(text: str) -> str:
    """Fallback text for notifications and screen readers."""
    return text.replace("*", "")
