"""Smoke test: one LOCAL_ONLY prompt through Router -> Executor -> Ollama.

Usage (from packages/core):  python3 scripts/first_run.py [--model NAME]

API routes use FakeClients, so nothing leaves the machine.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sensei_core.clients import OllamaClient, OllamaUnavailable, choose_local_model  # noqa: E402
from sensei_core.config import load_models  # noqa: E402
from sensei_core.executor import Executor  # noqa: E402
from sensei_core.router import FakeClient, Router  # noqa: E402
from sensei_core.trace import TraceWriter  # noqa: E402

PROMPT = "In one sentence, what is a permission tier?"


def pick_model(requested: str | None, configured: str) -> str:
    """Return a pulled model name, or exit with what is missing."""
    try:
        model, note = choose_local_model(requested, configured)
    except OllamaUnavailable as e:
        sys.exit(str(e))
    if note:
        print(f"note: {note}")
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=os.environ.get("SENSEI_LOCAL_MODEL"))
    args = parser.parse_args()

    routes = load_models()
    model = pick_model(args.model, routes["local_private"].model)
    # Record the model that actually answers, so the trace stays truthful.
    routes["local_private"] = dataclasses.replace(routes["local_private"], model=model)

    trace_path = Path(tempfile.gettempdir()) / "sensei" / "first_run.jsonl"
    trace = TraceWriter(trace_path, run_id="first_run", session_id="smoke")
    executor = Executor(
        Router(routes, trace=trace),
        {
            "local_private": OllamaClient(model),
            "cheap_api": FakeClient("fake cheap_api response"),
            "frontier_api": FakeClient("fake frontier_api response"),
        },
        trace=trace,
    )

    try:
        result = executor.run_once(PROMPT, "LOCAL_ONLY", "simple")
    except OllamaUnavailable as e:
        sys.exit(f"Ollama call failed: {e}")

    print(f"prompt:   {PROMPT}")
    print(f"response: {result.response.strip()}")
    print(f"route:    {result.route} (ollama model: {model})")
    print(f"latency:  {result.latency_ms} ms")
    print(f"trace:    {trace_path}")


if __name__ == "__main__":
    main()
