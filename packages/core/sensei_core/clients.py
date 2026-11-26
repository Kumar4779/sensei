"""Real model clients implementing the ModelClient protocol (router.py).

Standard library only. FakeClient lives in router.py.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable

from sensei_core.config import Route
from sensei_core.credentials import KIMI_KEY_SERVICE, keychain_secret

OLLAMA_BASE_URL = "http://localhost:11434"
# Slice 35: Moonshot's OpenAI-compatible API, as documented at build time (see CHANGELOG).
KIMI_BASE_URL = "https://api.moonshot.ai/v1"
KIMI_CHAT_PATH = "/chat/completions"


class OllamaUnavailable(RuntimeError):
    """Raised when the Ollama server cannot serve a completion."""


def _post_json(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class OllamaClient:
    """Calls a local Ollama server's /api/generate endpoint."""

    def __init__(
        self,
        model: str,
        base_url: str = OLLAMA_BASE_URL,
        timeout: float = 120.0,
        think: bool | None = None,
    ) -> None:
        self.model = model
        self.url = base_url.rstrip("/") + "/api/generate"
        self.timeout = timeout
        self.think = think  # None: leave the field out and use the server default

    @classmethod
    def for_route(cls, route: Route, **kwargs: Any) -> "OllamaClient":
        """Build a client from route config (model and think), per Principle 1."""
        return cls(route.model, think=route.think, **kwargs)

    def request_body(self, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "prompt": prompt, "stream": False}
        if self.think is not None:
            body["think"] = self.think
        return body

    def complete(self, prompt: str) -> str:
        body = self.request_body(prompt)
        try:
            data = _post_json(self.url, body, self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace").strip()
            raise OllamaUnavailable(
                f"Ollama returned HTTP {e.code} for model {self.model!r}: {detail}"
            ) from e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise OllamaUnavailable(f"cannot reach Ollama at {self.url}: {reason}") from e

        if "error" in data:
            raise OllamaUnavailable(f"Ollama error for model {self.model!r}: {data['error']}")
        response = data.get("response")
        if not isinstance(response, str):
            raise OllamaUnavailable(f"Ollama reply for model {self.model!r} had no 'response' text")
        return response


class KimiUnavailable(RuntimeError):
    """The Kimi route cannot serve a completion. error_class names why (traced in route_failure)."""

    fail_closed = False  # a route failure: the Executor's retry and escalation rules apply

    def __init__(self, message: str, error_class: str) -> None:
        super().__init__(message)
        self.error_class = error_class


class KimiKeyMissing(KimiUnavailable):
    """No Kimi key in the Keychain: the route is unavailable, and the run stops (never re-routed)."""

    fail_closed = True

    def __init__(self) -> None:
        super().__init__(f"Kimi API key not found in the macOS Keychain (service {KIMI_KEY_SERVICE!r}, "
                         "account 'sensei'); the cheap_api route is unavailable. Add it with: security "
                         f"add-generic-password -a sensei -s {KIMI_KEY_SERVICE} -U -w", "key_absent")


class KimiQuotaExhausted(KimiUnavailable):
    """HTTP 429 exceeded_current_quota_error: the account has no balance. A state, not a burst.

    Retrying cannot succeed until the owner tops up, so the run stops (Slice 36). Any other 429
    is a rate limit, transient, and keeps the retry-then-escalate rules.
    """

    fail_closed = True

    def __init__(self, model: str) -> None:
        super().__init__(f"Kimi returned HTTP 429 for model {model!r} (exceeded_current_quota_error): the "
                         "Moonshot account's quota or balance is exhausted; the cheap_api route is unavailable "
                         "until it is topped up", "quota_exhausted")


QUOTA_EXHAUSTED = "exceeded_current_quota_error"


def _http_error_class(status: int) -> str:
    if status in (401, 403):
        return "auth_failed"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "server_error"
    return "bad_request"


class KimiClient:
    """Calls Moonshot's chat-completions endpoint for one route. Standard library only.

    The key is fetched from the credential broker on every call and held only for
    that request. It never appears in an error message, so it cannot reach a trace
    (route_failure, run_escalated), the egress log, or a gateway response.
    """

    destination_host = "api.moonshot.ai"  # the egress log's host (Slice 23)

    def __init__(
        self,
        model: str,
        *,
        base_url: str = KIMI_BASE_URL,
        timeout: float = 120.0,
        max_completion_tokens: int = 4096,
        reasoning_effort: str | None = "low",
        key: Callable[[], str | None] = lambda: keychain_secret(KIMI_KEY_SERVICE),
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.model = model
        self.url = base_url.rstrip("/") + KIMI_CHAT_PATH
        self.timeout = timeout
        self.max_completion_tokens = max_completion_tokens  # bounds one call's output, so its cost
        self.reasoning_effort = reasoning_effort  # K3 always thinks; None leaves the server default
        self._key = key
        self._open = opener

    @classmethod
    def for_route(cls, route: Route, **kwargs: Any) -> "KimiClient":
        return cls(route.model, **kwargs)

    def request_body(self, prompt: str) -> dict[str, Any]:
        # temperature, top_p and n are fixed by Moonshot for K3-series models; they are left out.
        body: dict[str, Any] = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                                "stream": False, "max_completion_tokens": self.max_completion_tokens}
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    def complete(self, prompt: str) -> str:
        key = self._key()
        if not key:
            raise KimiKeyMissing()
        request = urllib.request.Request(
            self.url,
            data=json.dumps(self.request_body(prompt)).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            # Only the status and Moonshot's error type reach the message: never the body
            # verbatim (it could echo the request), never the key.
            kind = _error_type(e)
            if e.code == 429 and kind == QUOTA_EXHAUSTED:
                raise KimiQuotaExhausted(self.model) from None
            shown = f" ({kind})" if kind else ""
            raise KimiUnavailable(f"Kimi returned HTTP {e.code} for model {self.model!r}{shown}",
                                  _http_error_class(e.code)) from None
        except (TimeoutError, socket.timeout) as e:
            raise KimiUnavailable(f"Kimi call timed out after {self.timeout:g} s", "timeout") from e
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise KimiUnavailable(f"Kimi call timed out after {self.timeout:g} s", "timeout") from None
            raise KimiUnavailable(f"cannot reach Kimi at {self.destination_host}: {type(reason).__name__}",
                                  "unreachable") from None
        try:
            content = json.loads(raw.decode("utf-8"))["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            content = None
        if not isinstance(content, str) or not content:
            raise KimiUnavailable(f"Kimi reply for model {self.model!r} had no message content", "bad_response")
        return content


def _error_type(e: urllib.error.HTTPError) -> str:
    """Moonshot's error.type, if it is a plain identifier; never the message (it can echo input)."""
    try:
        err = json.loads(e.read().decode("utf-8")).get("error", {})
        kind = err.get("type") if isinstance(err, dict) else None
    except (ValueError, AttributeError, OSError):
        kind = None
    return kind if isinstance(kind, str) and kind.replace("_", "").isalnum() else ""


def list_ollama_models(base_url: str = OLLAMA_BASE_URL, timeout: float = 5.0) -> list[str]:
    """Return the names of locally pulled models; raise OllamaUnavailable if unreachable."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        raise OllamaUnavailable(f"cannot reach Ollama at {base_url}: {reason}") from e
    return [m["name"] for m in data.get("models", []) if "name" in m]


def choose_local_model(requested: str | None, configured: str) -> tuple[str, str | None]:
    """Pick a pulled Ollama model: requested, else configured, else any Qwen, else the first.

    Returns (model, note); note explains a fallback. Raises OllamaUnavailable
    saying exactly what is missing.
    """
    import shutil

    if shutil.which("ollama") is None:
        raise OllamaUnavailable("Ollama is not installed: install it from https://ollama.com, then run `ollama serve`.")
    try:
        pulled = list_ollama_models()
    except OllamaUnavailable as e:
        raise OllamaUnavailable(f"Ollama server is not running ({e}). Start it with `ollama serve` or the Ollama app.") from None
    if not pulled:
        raise OllamaUnavailable("Ollama is running but no model is pulled (`ollama list` is empty). Try `ollama pull qwen3`.")
    if requested:
        if requested not in pulled:
            raise OllamaUnavailable(f"Model {requested!r} is not pulled. Pulled models: {', '.join(pulled)}")
        return requested, None
    if configured in pulled:
        return configured, None
    for name in pulled:
        if name.startswith("qwen"):
            return name, None
    return pulled[0], (
        f"configured model {configured!r} and no Qwen model are pulled; "
        f"using {pulled[0]!r}. Pulled: {', '.join(pulled)}"
    )
