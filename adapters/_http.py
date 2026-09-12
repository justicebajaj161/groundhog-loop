"""Shared adapter plumbing: credential checks, dry-run gating, HTTP.

Both TicketAdapter and NotifierAdapter inherit from AdapterBase so the
dry-run switch is implemented exactly once. An adapter cannot forget to
honour it, because the only way it reaches the network is _request().

Dry-run does not stub the adapter out. It gates the socket write alone: the
call is built, logged in full, and answered with a synthesised response in
the *real* API's shape, so the response-parsing code in each adapter runs
identically whether or not credentials are present.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

import requests

import config

log = logging.getLogger(__name__)

# Every outbound call, real or dry-run, in order. demo.py reads this to
# render a traceable timeline; it is bounded so long runs cannot grow it
# without limit.
CALL_LOG: list[dict] = []
_MAX_CALLS = 500


class AdapterError(RuntimeError):
    """A backend rejected a call. Managers catch these per-adapter."""


def recent_calls(since: int = 0) -> list[dict]:
    return CALL_LOG[since:]


def call_count() -> int:
    return len(CALL_LOG)


def clear_calls() -> None:
    CALL_LOG.clear()


class AdapterBase(ABC):
    name: str = "base"

    @abstractmethod
    def missing_config(self) -> list[str]:
        """Names of the settings this adapter needs but does not have."""

    def is_configured(self) -> bool:
        return not self.missing_config()

    @property
    def dry_run(self) -> bool:
        """DRY_RUN=true/false forces; unset decides per adapter.

        This is what lets Jira run live against a real instance while Teams,
        whose webhook you have not set up yet, stays mocked in the same run.
        """
        if config.DRY_RUN is not None:
            return bool(config.DRY_RUN)
        return not self.is_configured()

    def status_note(self) -> str:
        if self.dry_run:
            missing = self.missing_config()
            why = f"missing {', '.join(missing)}" if missing else "DRY_RUN=true"
            return f"dry-run ({why})"
        return "live"

    # -- HTTP ------------------------------------------------------------

    def _dry_run_response(self, method: str, url: str, body: dict | None, kind: str) -> dict:
        """Synthesise what the real API would have returned. Override me."""
        return {}

    def _sdk_call(self, kind: str, target: str, payload: dict, fn):
        """Dry-run gate for adapters that talk through a vendor SDK.

        Same contract as _request, for backends (Slack) where we use the
        official client rather than raw HTTP.
        """
        record = {
            "adapter": self.name,
            "kind": kind,
            "method": "SDK",
            "url": target,
            "body": payload,
            "dry_run": self.dry_run,
        }
        if self.dry_run:
            record["response"] = self._dry_run_response("SDK", target, payload, kind)
            _remember(record)
            log.info(
                "[DRY-RUN] %s %s -> %s\n%s",
                self.name,
                kind,
                target,
                json.dumps(payload, indent=2, default=str)[:2000],
            )
            return record["response"]

        try:
            result = fn()
        except Exception as exc:
            record["error"] = str(exc)
            _remember(record)
            raise AdapterError(f"{self.name}: {kind} to {target} failed: {exc}") from exc

        # Vendor SDKs wrap the JSON body rather than being a mapping themselves --
        # slack_sdk's SlackResponse exposes it as .data and has no .keys(), so the
        # naive dict() branch stringified the entire live response and dropped "ts".
        # That made the LIVE path parse differently from the dry-run path, which is
        # exactly the invariant this module exists to preserve.
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            record["response"] = dict(data)
        elif hasattr(result, "keys"):
            record["response"] = dict(result)
        else:
            record["response"] = {"result": str(result)}
        _remember(record)
        return record["response"]

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        headers: dict | None = None,
        auth=None,
        params: dict | None = None,
        kind: str = "",
    ) -> dict:
        record = {
            "adapter": self.name,
            "kind": kind,
            "method": method.upper(),
            "url": url,
            "body": json_body,
            "dry_run": self.dry_run,
        }

        if self.dry_run:
            record["response"] = self._dry_run_response(method, url, json_body, kind)
            _remember(record)
            log.info(
                "[DRY-RUN] %s %s %s\n%s",
                self.name,
                method.upper(),
                url,
                json.dumps(json_body, indent=2)[:2000] if json_body else "(no body)",
            )
            return record["response"]

        try:
            response = requests.request(
                method.upper(),
                url,
                json=json_body,
                headers=headers,
                auth=auth,
                params=params,
                timeout=config.HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            record["error"] = str(exc)
            _remember(record)
            raise AdapterError(f"{self.name}: {method.upper()} {url} failed: {exc}") from exc

        record["status_code"] = response.status_code
        if not response.ok:
            record["error"] = response.text[:500]
            _remember(record)
            raise AdapterError(
                f"{self.name}: {method.upper()} {url} returned "
                f"{response.status_code}: {response.text[:300]}"
            )

        if not response.content:
            payload: dict = {}
        else:
            try:
                payload = response.json()
            except ValueError:
                payload = {"raw": response.text[:500]}

        record["response"] = payload
        _remember(record)
        return payload


def _remember(record: dict) -> None:
    CALL_LOG.append(record)
    if len(CALL_LOG) > _MAX_CALLS:
        del CALL_LOG[: len(CALL_LOG) - _MAX_CALLS]
