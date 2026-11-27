"""Run the "Talk to Sensei" demo.

    SENSEI_DEMO_TOKEN=... python -m sensei_demo              # LIVE SPEECH: keys from the macOS Keychain
    SENSEI_DEMO_TOKEN=... python -m sensei_demo --simulated  # typed input, fake adapters, no keys

Hosted (Demo Slice 2, Fly.io): keys come from the server's environment, chosen explicitly,
and the client address from the proxy's header:

    python -m sensei_demo --keys env --state-dir /data --trusted-ip-header Fly-Client-IP

Live is the default and fails closed: if either key is missing it refuses to
start and says so, instead of falling back to simulation. Then open
http://127.0.0.1:8766/demo and enter the token.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from sensei_core.clients import KimiClient
from sensei_core.config import load_models

from voice.speech import KEY_SOURCES, SpeechUnavailable, make_stt, make_tts

import sensei_demo.server as server
from sensei_demo.server import DEFAULT_PORT, TOKEN_ENV, DemoConfigError, Limits, bind_host, create_app

KIMI_KEY_ENV = "KIMI_API_KEY"  # the hosted path's Moonshot key (--keys env); locally, core's Keychain item


def model_client(keys: str) -> KimiClient:
    """Core's own Kimi client for the cheap_api route (#25). With --keys env the key is read per call from
    KIMI_API_KEY; otherwise core's default reads the Keychain. No fallback between the two."""
    route = load_models()["cheap_api"]
    if keys == "env":
        return KimiClient.for_route(route, timeout=20.0, key=lambda: os.environ.get(KIMI_KEY_ENV, "").strip() or None)
    return KimiClient.for_route(route, timeout=20.0)


def main(argv: list[str] | None = None) -> int:
    defaults = Limits()
    parser = argparse.ArgumentParser(description="Talk to Sensei: browser voice with a live permission ledger.")
    parser.add_argument("--simulated", action="store_true",
                        help="typed input and fake adapters (development without keys); shown on the page")
    parser.add_argument("--keys", choices=KEY_SOURCES, default="keychain",
                        help="where the provider keys come from: the macOS Keychain (default) or the environment "
                             "(DEEPGRAM_API_KEY, ELEVENLABS_API_KEY), for a host without a Keychain")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SENSEI_DEMO_PORT", DEFAULT_PORT)))
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="where the daily spend counter and traces live (default ~/.sensei/demo)")
    parser.add_argument("--trace-dir", type=Path, default=None, help="default <state-dir>/traces")
    parser.add_argument("--max-usd", type=float, default=1.00, help="refuse live turns once this run's spend reaches this")
    parser.add_argument("--daily-usd", type=float, default=defaults.daily_usd,
                        help="refuse live turns once today's (UTC) spend, across all runs, reaches this")
    parser.add_argument("--max-sessions", type=int, default=defaults.max_sessions, help="simultaneous sessions")
    parser.add_argument("--trusted-ip-header", default=None,
                        help="read the client address from this proxy header (e.g. Fly-Client-IP); off by default")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    state_dir = args.state_dir or server.DEFAULT_STATE_DIR
    trace_dir = args.trace_dir or state_dir / "traces"
    limits = Limits(daily_usd=args.daily_usd, max_sessions=args.max_sessions, trusted_ip_header=args.trusted_ip_header)
    try:
        if args.simulated:
            app = create_app("simulated", trace_dir=trace_dir, state_dir=state_dir, limits=limits)
        else:
            keys = args.keys
            make_stt("deepgram", key_source=keys), make_tts("elevenlabs", key_source=keys, player=lambda a: None)
            # smart_format: spoken numbers arrive as digits ("invoice 204"), which the pay card needs.
            # Free-form questions go to core's model backend (#25); a missing Moonshot key isn't fatal here: the
            # visitor hears that the model is unreachable, and the ledger shows why.
            app = create_app("live", stt_factory=lambda: make_stt("deepgram", key_source=keys, smart_format=True),
                             tts_factory=lambda player: make_tts("elevenlabs", key_source=keys, player=player),
                             trace_dir=trace_dir, state_dir=state_dir, max_usd=args.max_usd, limits=limits,
                             model_client=model_client(keys))
    except SpeechUnavailable as e:
        print(f"sensei-demo: {e}\n  (run with --simulated to develop without keys)", file=sys.stderr)
        return 2
    except DemoConfigError as e:
        print(f"sensei-demo: {e}\n  (set {TOKEN_ENV}, e.g. export {TOKEN_ENV}=$(openssl rand -hex 24))", file=sys.stderr)
        return 2
    import uvicorn

    host = bind_host()
    print(f"Talk to Sensei ({'SIMULATED' if args.simulated else 'LIVE SPEECH'}, keys: "
          f"{'none' if args.simulated else args.keys}): http://{host}:{args.port}/demo", flush=True)
    uvicorn.run(app, host=host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
