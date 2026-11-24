"""Real model clients implementing the ModelClient protocol (router.py).

Standard library only. FakeClient lives in router.py.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from sensei_core.config import Route

OLLAMA_BASE_URL = "http://localhost:11434"


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
