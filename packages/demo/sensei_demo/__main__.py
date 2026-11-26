"""Run the "Talk to Sensei" demo locally.

    SENSEI_DEMO_TOKEN=... python -m sensei_demo              # LIVE SPEECH: needs both Keychain keys
    SENSEI_DEMO_TOKEN=... python -m sensei_demo --simulated  # typed input, fake adapters, no keys

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

from voice.speech import SpeechUnavailable, make_stt, make_tts

from sensei_demo.server import (DEFAULT_PORT, DEFAULT_TRACE_DIR, TOKEN_ENV, DemoConfigError, bind_host,
                                create_app)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Talk to Sensei: browser voice with a live permission ledger.")
    parser.add_argument("--simulated", action="store_true",
                        help="typed input and fake adapters (development without keys); shown on the page")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SENSEI_DEMO_PORT", DEFAULT_PORT)))
    parser.add_argument("--trace-dir", type=Path, default=DEFAULT_TRACE_DIR)
    parser.add_argument("--max-usd", type=float, default=1.00, help="refuse live turns once estimated spend reaches this")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.simulated:
            app = create_app("simulated", trace_dir=args.trace_dir)
        else:
            make_stt("deepgram"), make_tts("elevenlabs", player=lambda audio: None)  # key check, before serving
            # smart_format: spoken numbers arrive as digits ("invoice 204"), which the pay card needs.
            app = create_app("live", stt_factory=lambda: make_stt("deepgram", smart_format=True),
                             tts_factory=lambda player: make_tts("elevenlabs", player=player),
                             trace_dir=args.trace_dir, max_usd=args.max_usd)
    except SpeechUnavailable as e:
        print(f"sensei-demo: {e}\n  (run with --simulated to develop without keys)", file=sys.stderr)
        return 2
    except DemoConfigError as e:
        print(f"sensei-demo: {e}\n  (set {TOKEN_ENV}, e.g. export {TOKEN_ENV}=$(openssl rand -hex 24))", file=sys.stderr)
        return 2
    import uvicorn

    host = bind_host()
    print(f"Talk to Sensei ({'SIMULATED' if args.simulated else 'LIVE SPEECH'}): http://{host}:{args.port}/demo")
    uvicorn.run(app, host=host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
