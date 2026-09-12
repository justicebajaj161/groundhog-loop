#!/usr/bin/env python3
"""Socket Mode listener -- the receiving half of the Slack integration.

The adapters only send. This process receives: button clicks, free-text
replies, and the /groundhogloop slash command. Socket Mode means no public URL,
no ngrok and no deploy -- it dials out to Slack over a websocket, so it works
from a laptop.

Setup (api.slack.com/apps):
  1. Socket Mode -> Enable, generate an app-level token (xapp-...) with
     connections:write  ->  SLACK_APP_TOKEN
  2. OAuth & Permissions -> bot scopes: chat:write, im:write, users:read,
     users:read.email, commands  ->  install, copy xoxb-...  ->  SLACK_BOT_TOKEN
  3. Interactivity -> on (Socket Mode needs no request URL)
  4. Event Subscriptions -> subscribe to bot events: message.im
  5. Slash Commands -> /groundhogloop

    python slack_listener.py
"""

from __future__ import annotations

import logging
import re
import sys

import config
import pipeline
import scheduler
import storage
from adapters.notifier_base import STATUS_ACTIONS

log = logging.getLogger(__name__)

# Free-text replies name the item they are about, e.g. "#12 done" or a reply
# in the thread of a message that mentioned item #12.
_ITEM_RE = re.compile(r"#(\d+)")


def build_app():
    from slack_bolt import App

    if not config.SLACK_BOT_TOKEN:
        raise SystemExit("SLACK_BOT_TOKEN is not set -- see the setup notes in this file.")
    if not config.SLACK_APP_TOKEN:
        raise SystemExit("SLACK_APP_TOKEN (xapp-...) is not set -- Socket Mode needs it.")

    app = App(token=config.SLACK_BOT_TOKEN, signing_secret=config.SLACK_SIGNING_SECRET)

    # Share this app's client with the sending side so the live process holds
    # one connection rather than two.
    from adapters.notifier_manager import NotifierManager
    from adapters.slack_adapter import SlackAdapter

    notifiers = NotifierManager()
    for adapter in notifiers.adapters:
        if isinstance(adapter, SlackAdapter):
            adapter._client = app.client

    register_handlers(app, notifiers)
    return app


def register_handlers(app, notifiers) -> None:
    # -- structured quick replies -------------------------------------
    # One handler per button. These set status directly and never reach the
    # LLM, which is the whole point of offering buttons.
    def make_button_handler(status: str):
        def handler(ack, body, respond):
            ack()
            item_id = int(body["actions"][0]["value"])
            item = pipeline.apply_structured_reply(item_id, status)
            if item is None:
                respond(f"Couldn't find item #{item_id}.")
                return
            label = dict(STATUS_ACTIONS).get(status, status)
            respond(f"Thanks -- item #{item_id} is now *{label}*.\n> {item['item_text']}")
            log.info("item %s -> %s via button", item_id, status)

        return handler

    for status, _label in STATUS_ACTIONS:
        app.action(f"status_{status}")(make_button_handler(status))

    # -- free-text replies --------------------------------------------
    @app.event("message")
    def handle_message(event, say, logger):
        if event.get("bot_id") or event.get("subtype"):
            return  # ignore our own messages and edits/joins
        text = (event.get("text") or "").strip()
        if not text:
            return

        item_id = _resolve_item(event, text)
        if item_id is None:
            say(
                "I'm not sure which item that's about. Reply in the thread of a "
                "nudge, or start your message with the item number, like `#12 done`."
            )
            return

        try:
            result = pipeline.apply_text_reply(item_id, text)
        except Exception as exc:
            # A handler that raises loses the reply silently, so tell the
            # person their message landed and what went wrong.
            logger.exception("free-text classification failed")
            say(
                f"I couldn't classify that one ({exc}). Item #{item_id} is unchanged "
                "-- the Done / In progress / Blocked buttons still work."
            )
            return
        if result is None:
            say(f"Couldn't find item #{item_id}.")
            return
        classification = result["classification"]
        note = f" -- {classification['note']}" if classification.get("note") else ""
        say(f"Got it. Item #{item_id} marked *{classification['status']}*{note}.")
        log.info("item %s -> %s via text", item_id, classification["status"])

    # -- slash command -------------------------------------------------
    @app.command("/groundhogloop")
    def handle_command(ack, command, respond):
        ack()
        respond(scheduler.handle_config_command(command.get("text", "")))


def _resolve_item(event: dict, text: str) -> int | None:
    """Work out which action item a free-text reply refers to."""
    match = _ITEM_RE.search(text)
    if match:
        return int(match.group(1))

    # Otherwise: the newest open item belonging to this user, which is almost
    # always what someone replying to a DM nudge means.
    user = event.get("user")
    if not user:
        return None
    candidates = [
        item
        for item in storage.open_items()
        if (item.get("owner") or "").lower() in _aliases(user)
    ]
    return candidates[-1]["id"] if candidates else None


def _aliases(user_id: str) -> set[str]:
    """Names in SLACK_USER_MAP that point at this member ID."""
    names = {name for name, mapped in config.SLACK_USER_MAP.items() if mapped == user_id}
    names.add(user_id.lower())
    return names


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError:
        print("Needs slack-bolt: pip install slack-bolt", file=sys.stderr)
        return 1

    app = build_app()
    log.info(
        "connecting over Socket Mode -- buttons and replies will reach this process. "
        "Ctrl-C to stop."
    )
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
