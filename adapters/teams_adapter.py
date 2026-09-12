"""Microsoft Teams backend (Power Automate Workflows webhook).

The classic Office 365 connector webhooks that most Teams integrations used
were retired by Microsoft on 18-22 May 2026 and no longer deliver. The
supported replacement is a Power Automate "Workflows" webhook, which accepts
the same POST shape: an Adaptive Card wrapped in a message attachment.

To create one: in the Teams channel, ... -> Workflows -> "Post to a channel
when a webhook request is received". Copy the generated URL into
TEAMS_CHANNEL_WEBHOOK_URL. For DMs, create a second flow with "Post to a chat"
and use TEAMS_DM_WEBHOOK_URL.

Workflows webhooks are one-way, so this backend has no interactive buttons --
it asks for a free-text reply and sets supports_buttons = False. The
NotifierAdapter interface is unchanged, so a Bot Framework / M365 Agents SDK
backend could replace this transport later without touching any caller.
"""

from __future__ import annotations

import itertools
import logging

import config
from adapters.notifier_base import STATUS_ACTIONS, Delivery, NotifierAdapter

log = logging.getLogger(__name__)

_fake_ids = itertools.count(1)


def _to_markdown(text: str) -> str:
    """Slack-style *bold* -> Adaptive Card **bold**."""
    out, bold = [], False
    for char in text:
        if char == "*":
            out.append("**")
            bold = not bold
        else:
            out.append(char)
    return "".join(out)


def adaptive_card(text: str, item: dict | None = None) -> dict:
    """Render a message as an Adaptive Card payload."""
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": _to_markdown(text),
            "wrap": True,
        }
    ]

    actions: list[dict] = []
    if item:
        for key, value in (item.get("ticket_refs") or {}).items():
            if key.endswith("_url") and value:
                actions.append(
                    {"type": "Action.OpenUrl", "title": f"Open {key[:-4]}", "url": value}
                )
        if item.get("id") is not None:
            # No interactive buttons over a one-way webhook, so tell people
            # exactly what to type instead -- the reply is classified by the LLM.
            options = " / ".join(label for _s, label in STATUS_ACTIONS)
            body.append(
                {
                    "type": "TextBlock",
                    "text": f"_Reply with_ `#{item['id']} <{options}>` _or just describe "
                    "where it stands._",
                    "wrap": True,
                    "isSubtle": True,
                    "spacing": "Medium",
                }
            )

    card: dict = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }
    if actions:
        card["actions"] = actions

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


class TeamsAdapter(NotifierAdapter):
    name = "teams"
    # A Workflows webhook cannot receive a button callback.
    supports_buttons = False

    def missing_config(self) -> list[str]:
        if not config.TEAMS_CHANNEL_WEBHOOK_URL:
            return ["TEAMS_CHANNEL_WEBHOOK_URL"]
        return []

    def default_channel(self) -> str:
        return config.TEAMS_TEAM_CHANNEL

    def _dry_run_response(self, method: str, url: str, body: dict | None, kind: str) -> dict:
        return {"status": "accepted", "id": f"teams-{next(_fake_ids):04d}"}

    def resolve_user(self, user: str) -> str | None:
        """Map a transcript first name to a UPN/email the flow can route to."""
        if not user:
            return None
        candidate = user.strip()
        if "@" in candidate:
            return candidate
        return config.TEAMS_USER_MAP.get(candidate.lower())

    # -- contract ---------------------------------------------------------

    def post_message(self, channel: str, text: str, *, item: dict | None = None) -> Delivery:
        target = channel or self.default_channel()
        url = config.TEAMS_CHANNEL_WEBHOOK_URL or "https://prod-00.westus.logic.azure.com/<not-set>"
        payload = adaptive_card(text, item)
        self._request("POST", url, json_body=payload, kind="channel_post")
        return Delivery(self.name, target, ok=True, detail="adaptive card")

    def dm_user(self, user: str, text: str, *, item: dict | None = None) -> Delivery:
        recipient = self.resolve_user(user)
        url = config.TEAMS_DM_WEBHOOK_URL

        if not url or not recipient:
            # Degrade to the team channel rather than dropping the nudge --
            # and say so, because a silently-swallowed DM is worse than a
            # noisy channel post.
            reason = (
                "TEAMS_DM_WEBHOOK_URL is not set"
                if not url
                else f"{user!r} is not in TEAMS_USER_MAP"
            )
            log.warning("teams: cannot DM (%s) -- posting to %s instead", reason, self.default_channel())
            delivery = self.post_message(self.default_channel(), f"For {user}:\n{text}", item=item)
            delivery.detail = f"fallback to channel ({reason})"
            return delivery

        payload = adaptive_card(text, item)
        # The flow reads these to decide who to open the chat with.
        payload["recipient"] = recipient
        payload["subject"] = f"Action item follow-up for {user}"
        self._request("POST", url, json_body=payload, kind="dm")
        return Delivery(self.name, f"{user} <{recipient}>", ok=True, detail="dm")
