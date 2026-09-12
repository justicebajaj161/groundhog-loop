"""The single place this codebase talks to a model provider.

Everything goes through OpenRouter's OpenAI-compatible API, so swapping
``deepseek/deepseek-chat`` for ``anthropic/claude-sonnet-4.5`` or
``openai/gpt-4o-mini`` is a one-line change in .env and nothing else.

OpenRouter exposes *both* endpoints we need on the same base URL and key:
    POST /chat/completions
    POST /embeddings          (embedding models are catalogued separately,
                               at GET /embeddings/models -- they deliberately
                               do not appear in GET /models)
"""

from __future__ import annotations

import json
import logging
import re
import time

from openai import OpenAI

import config

log = logging.getLogger(__name__)

_client: OpenAI | None = None

_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


class LLMError(RuntimeError):
    """Raised when the provider call or its response could not be used."""


def get_client() -> OpenAI:
    """Lazily build the shared client.

    Lazy on purpose: importing this module without an API key must not
    explode, so that ``--fake-llm`` and the offline unit checks still run.
    """
    global _client
    if _client is None:
        if not config.OPENROUTER_API_KEY:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Copy .env.example to .env and add "
                "your key, or run demo.py with --fake-llm to skip live model calls."
            )
        _client = OpenAI(
            base_url=config.OPENROUTER_BASE_URL,
            api_key=config.OPENROUTER_API_KEY,
            default_headers={
                "HTTP-Referer": config.OPENROUTER_REFERER,
                "X-Title": config.OPENROUTER_TITLE,
            },
        )
    return _client


def reset_client() -> None:
    """Drop the cached client so a config change takes effect."""
    global _client
    _client = None


# --------------------------------------------------------------------------
# JSON-shaped chat
# --------------------------------------------------------------------------


def _extract_json(text: str):
    """Parse JSON out of a model response that may be dressed up.

    Handles bare JSON, ```json fenced blocks, and prose wrapped around a
    JSON body. Raises ValueError if nothing parseable is found.
    """
    if text is None:
        raise ValueError("empty response")

    candidate = text.strip()

    # Strip a leading ```json / trailing ``` fence if present.
    if candidate.startswith("```"):
        candidate = _FENCE_RE.sub("", candidate).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to slicing from the first bracket to its matching close,
    # which survives a model that prefixed "Here is the JSON:".
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise ValueError(f"no parseable JSON in model response: {text[:300]!r}")


def chat_json(system_prompt: str, user_prompt: str, *, retries: int | None = None) -> dict:
    """Ask the model for a JSON *object* and return it parsed.

    Always request an object rather than a bare array: provider JSON mode
    rejects a top-level array on several backends, and that is precisely the
    failure that shows up only after someone swaps LLM_MODEL.
    """
    client = get_client()
    attempts = config.LLM_MAX_RETRIES if retries is None else retries
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    kwargs: dict = {
        "model": config.LLM_MODEL,
        "temperature": config.LLM_TEMPERATURE,
    }
    if config.LLM_JSON_MODE:
        kwargs["response_format"] = {"type": "json_object"}
        if config.OPENROUTER_REQUIRE_PARAMETERS:
            # Keep OpenRouter off provider endpoints that do not implement
            # response_format, while leaving cross-provider fallback on. The
            # fallback below still exists for providers that accept the
            # parameter and then reject the feature.
            kwargs["extra_body"] = {
                "provider": {"require_parameters": True, "allow_fallbacks": True}
            }

    last_error: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            response = client.chat.completions.create(messages=messages, **kwargs)
        except Exception as exc:  # provider/network failure
            last_error = exc
            # JSON mode is not universally supported, so retry once without it
            # rather than failing a model swap outright -- but only when the
            # provider actually objected to the parameter. Dropping it on any
            # error (a 401, a timeout) would silently disable structured
            # output for the rest of the process.
            # The provider PREFERENCE is an optimisation, not a requirement. If
            # no endpoint satisfies it, OpenRouter 404s with "No endpoints
            # found" -- which is not a 400/422 and so never reaches the JSON
            # fallback below. Left unhandled that turns an ordinary model swap
            # into a hard failure (observed with openai/gpt-5-mini and
            # anthropic/claude-sonnet-5, whose endpoints do not advertise the
            # parameters we send). Drop the preference and let routing proceed.
            if "extra_body" in kwargs and _is_routing_rejection(exc):
                log.warning(
                    "no endpoint satisfies the provider preference for %s, retrying "
                    "without it",
                    config.LLM_MODEL,
                )
                kwargs.pop("extra_body")
                continue
            if "response_format" in kwargs and _is_json_mode_rejection(exc):
                log.warning("JSON mode rejected by %s, retrying without it", config.LLM_MODEL)
                kwargs.pop("response_format")
                # The preference only ever existed to protect response_format.
                kwargs.pop("extra_body", None)
                continue
            log.warning("LLM call failed (attempt %s/%s): %s", attempt + 1, attempts + 1, exc)
            _backoff(exc, attempt, attempts)
            continue

        content = (response.choices[0].message.content or "").strip()
        try:
            parsed = _extract_json(content)
        except ValueError as exc:
            last_error = exc
            log.warning("unparseable JSON (attempt %s/%s)", attempt + 1, attempts + 1)
            _backoff(exc, attempt, attempts)
            messages = messages + [
                {"role": "assistant", "content": content[:2000]},
                {
                    "role": "user",
                    "content": "That was not valid JSON. Reply with the raw JSON object only "
                    "-- no prose, no markdown fences.",
                },
            ]
            continue

        if isinstance(parsed, list):
            # Tolerate a model that returned the array we told it to wrap.
            return {"items": parsed}
        if not isinstance(parsed, dict):
            last_error = ValueError(f"expected object, got {type(parsed).__name__}")
            continue
        return parsed

    raise LLMError(f"{config.LLM_MODEL} did not return usable JSON: {last_error}")


# --------------------------------------------------------------------------
# embeddings
# --------------------------------------------------------------------------


def _backoff(exc: Exception, attempt: int, attempts: int) -> None:
    """Wait before the next attempt. Immediate retries are not retries.

    A 429 from OpenRouter's shared free pool clears on the order of seconds, so
    hammering it three times in one second just burns every attempt -- which is
    exactly how a real recurrence link was lost on 2026-09-12.
    """
    if attempt >= attempts:  # last time round, nothing left to wait for
        return
    status = getattr(exc, "status_code", None)
    base = config.LLM_RETRY_429_DELAY if status == 429 else config.LLM_RETRY_BASE_DELAY
    delay = base * (2 ** attempt)
    log.warning("backing off %.1fs before retry %s/%s", delay, attempt + 2, attempts + 1)
    time.sleep(delay)


def _is_routing_rejection(exc: Exception) -> bool:
    """Did OpenRouter fail to find ANY endpoint matching our provider preference?

    Distinct from a model that does not exist: both are 404, so the message is
    what separates them. Only the routing funnel failure is recoverable by
    relaxing the preference.
    """
    if getattr(exc, "status_code", None) != 404:
        return False
    message = str(exc).lower()
    return "no endpoints found" in message or "failed_routing_step" in message


def _is_json_mode_rejection(exc: Exception) -> bool:
    """Did the provider object to response_format specifically?

    Anything else -- auth, rate limits, timeouts -- must not cause us to give
    up on structured output.
    """
    status = getattr(exc, "status_code", None)
    if status is not None and status not in (400, 422):
        return False
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "response_format",
            "json_object",
            "json mode",
            "json_schema",
            # Providers routed through OpenRouter often name the *feature* rather
            # than the parameter. Novita, for one, 400s a model swap with
            # "does not support feature: structured-outputs" and never says
            # "response_format" -- without these markers the fallback never fires
            # and the swap hard-fails, which is the exact case it exists for.
            "structured-outputs",
            "structured outputs",
            "structured_outputs",
            "structured output",
        )
    )


def _truncate(text: str) -> str:
    """Keep inputs inside the embedding model's context window.

    The default model (openai/text-embedding-3-small) allows 8192 tokens, so
    EMBEDDING_MAX_CHARS is no longer the tight constraint it was under the old
    free model's 512. It still matters: a model swap can reinstate a small
    window, and truncation is what stops that becoming a hard rejection.
    """
    limit = config.EMBEDDING_MAX_CHARS
    text = (text or "").strip()
    if limit > 0 and len(text) > limit:
        return text[:limit]
    return text


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed several strings in one request."""
    if not texts:
        return []
    client = get_client()
    payload: dict = {
        "model": config.EMBEDDING_MODEL,
        "input": [_truncate(t) for t in texts],
        "encoding_format": "float",
    }
    if config.EMBEDDING_DIMENSIONS > 0:
        payload["dimensions"] = config.EMBEDDING_DIMENSIONS

    attempts = config.LLM_MAX_RETRIES
    last_error: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            response = client.embeddings.create(**payload)
            break
        except Exception as exc:
            # Embeddings had no retry at all, so one transient 429 took down the
            # whole transcript -- every item in it, not just one adjudication.
            last_error = exc
            log.warning(
                "embedding call failed (attempt %s/%s): %s", attempt + 1, attempts + 1, exc
            )
            _backoff(exc, attempt, attempts)
    else:
        raise LLMError(
            f"embedding call to {config.EMBEDDING_MODEL} failed: {last_error}"
        ) from last_error

    # Order is not contractually guaranteed; sort by index to be safe.
    ordered = sorted(response.data, key=lambda d: d.index)
    return [list(d.embedding) for d in ordered]


def embed(text: str) -> list[float]:
    """Embed a single string."""
    vectors = embed_batch([text])
    if not vectors:
        raise LLMError("embedding endpoint returned no vectors")
    return vectors[0]
