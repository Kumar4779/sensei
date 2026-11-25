"""Gateway: Sensei's localhost HTTP API (DESIGN.md §2, Threat 7).

Token auth on every endpoint except /health, a kill switch that refuses
new runs, cost totals from the trace, and memory review/deletion.
All components are injected; main() wires a real local setup.

Slice 22 adds a phone-first web UI (a client of these endpoints, nothing
more) and the brief endpoints it needs. /ui itself is a public token-entry
page holding no data; the app it loads (/ui/app/*) and every data endpoint
require the token.

Run:  SENSEI_GATEWAY_TOKEN=... python -m sensei_core.gateway
Binds 127.0.0.1 only. SENSEI_GATEWAY_ALLOW_LAN=1 is an explicit opt-in to bind
all interfaces (same-LAN phone access); it defaults off and logs a warning.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from sensei_core.brief import build_brief, failed_labels, load_morning_fixture
from sensei_core.egress import EgressLog

from sensei_core.executor import BudgetExceeded, Executor, VerificationFailed
from sensei_core.ledger import CostLedger
from sensei_core.memory import MemoryStore
from sensei_core.trace import TraceWriter

TOKEN_ENV = "SENSEI_GATEWAY_TOKEN"
ALLOW_LAN_ENV = "SENSEI_GATEWAY_ALLOW_LAN"
UI_DIR = Path(__file__).with_name("ui")
# The authed app files the /ui page loads. Fixed list: no path is ever taken from the URL.
UI_APP_FILES = {"app.js": "text/javascript", "app.css": "text/css"}
log = logging.getLogger("sensei.gateway")


def _utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class GatewayConfigError(RuntimeError):
    """Raised when the gateway cannot start safely."""


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1)
    sensitivity_class: Literal["API_OK", "LOCAL_ONLY"]
    difficulty: Literal["simple", "standard", "hard"]
    max_cost_usd: float | None = Field(default=None, ge=0)


class ConfirmRequest(BaseModel):
    entry_id: str


def _error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


class _Unauthorized(Exception):
    pass


def create_app(
    executor: Executor,
    ledger_path: str | Path,
    memory_store: MemoryStore,
    trace: TraceWriter | None = None,
    *,
    brief_fixture: str | Path | None = None,
    brief_dir: str | Path | None = None,
    egress_path: str | Path | None = None,
) -> FastAPI:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        # Fail closed: never serve without auth.
        raise GatewayConfigError(f"{TOKEN_ENV} is not set; refusing to start the gateway without auth")

    app = FastAPI(title="Sensei Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    # Kill switch is in-memory app state (v1). Known limit: a restart clears it,
    # and it refuses new runs but does not interrupt a run already in flight.
    # Flagged for the threat model review.
    app.state.killed = False
    ledger_path = Path(ledger_path)

    def event(event_type: str, payload: dict) -> None:
        if trace is not None:
            trace.write(event_type, payload)

    def require_token(request: Request, x_sensei_token: str | None = Header(default=None)) -> None:
        if x_sensei_token is None or not hmac.compare_digest(x_sensei_token.encode(), token.encode()):
            event("gateway_auth_failed", {"path": request.url.path, "token_present": x_sensei_token is not None})
            raise _Unauthorized()

    @app.exception_handler(_Unauthorized)
    def unauthorized(_request: Request, _exc: _Unauthorized) -> JSONResponse:
        return _error(401, "unauthorized: missing or invalid X-Sensei-Token")

    auth = [Depends(require_token)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "killed": app.state.killed}

    @app.post("/run", dependencies=auth)
    def run(body: RunRequest):
        if app.state.killed:
            event("gateway_run_refused", {"reason": "kill switch engaged"})
            return _error(503, "kill switch engaged")
        try:
            result = executor.run_once(
                body.prompt, body.sensitivity_class, body.difficulty, max_cost_usd=body.max_cost_usd
            )
        except BudgetExceeded as e:
            return _error(
                402,
                "budget exceeded",
                accumulated_est_cost_usd=e.accumulated_cost_usd,
                limit_usd=e.limit_usd,
                next_call_est_cost_usd=e.next_call_est_usd,
            )
        except VerificationFailed as e:
            return _error(
                422, "verification failed", report=e.report.to_dict(), total_est_cost_usd=e.total_est_cost_usd
            )
        except Exception as e:
            return _error(502, f"run failed: {type(e).__name__}: {e}")
        return dataclasses.asdict(result)

    @app.post("/kill", dependencies=auth)
    def kill() -> dict:
        app.state.killed = True
        event("kill_switch_engaged", {})
        return {"killed": True}

    @app.post("/revive", dependencies=auth)
    def revive() -> dict:
        app.state.killed = False
        event("kill_switch_disengaged", {})
        return {"killed": False}

    def egress_today() -> dict:
        # Slice 23: today's API egress per destination (calls, bytes); never content.
        day = _utc_today()
        if egress_path is None:
            return {"day_utc": day, "by_destination": {}}
        return EgressLog(egress_path).summary(day)

    @app.get("/costs", dependencies=auth)
    def costs() -> dict:
        if not ledger_path.exists():
            return {"basis": "estimated", "total_est_cost_usd": 0.0, "by_route": {}, "by_day": {},
                    "egress": egress_today()}
        ledger = CostLedger(ledger_path)
        return {
            "basis": "estimated",
            "total_est_cost_usd": ledger.total_est_cost_usd,
            "by_route": ledger.by_route(),
            "by_day": ledger.by_day(),
            "egress": egress_today(),
        }

    @app.get("/memory/beliefs", dependencies=auth)
    def beliefs() -> list[dict]:
        return [{"id": e["id"], "fact": e["fact"], "category": e["category"]} for e in memory_store.list_beliefs()]

    @app.get("/memory/pending", dependencies=auth)
    def pending() -> list[dict]:
        keys = ("id", "fact", "category", "source", "origin", "status")
        return [{k: e[k] for k in keys} for e in memory_store.list_pending()]

    @app.post("/memory/confirm", dependencies=auth)
    def confirm(body: ConfirmRequest):
        try:
            entry = memory_store.confirm(body.entry_id)
        except KeyError:
            return _error(404, "memory entry not found")
        event("memory_confirmed", {"entry_id": entry["id"]})
        return {"id": entry["id"], "status": entry["status"]}

    # ---- Slice 22: costs for today, the morning brief, the web UI ------------

    @app.get("/costs/today", dependencies=auth)
    def costs_today() -> dict:
        day = _utc_today()
        by_route = CostLedger(ledger_path).by_route(day=day) if ledger_path.exists() else {}
        return {"basis": "estimated", "day_utc": day, "total_est_cost_usd": sum(by_route.values()),
                "by_route": by_route}

    briefs = Path(brief_dir) if brief_dir is not None else None

    def brief_paths(day: str) -> tuple[Path, Path]:
        assert briefs is not None
        return briefs / f"{day}.md", briefs / f"{day}.json"

    @app.post("/brief/run", dependencies=auth)
    def brief_run():
        if app.state.killed:
            event("gateway_run_refused", {"reason": "kill switch engaged", "operation": "brief"})
            return _error(503, "kill switch engaged")
        if brief_fixture is None or briefs is None:
            return _error(409, "no brief data source configured (SENSEI_BRIEF_FIXTURE)")
        day = date.today().isoformat()  # the user's local day
        md, status = brief_paths(day)
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        cost = CostLedger(ledger_path).by_day().get(yesterday, 0.0) if ledger_path.exists() else 0.0
        try:
            data = load_morning_fixture(brief_fixture, pending_count=len(memory_store.list_pending()),
                                        yesterday_cost_usd=cost)
            result = build_brief(executor, data, md, "LOCAL_ONLY", repair=True, assemble_inbox=True, trace=trace)
        except BudgetExceeded as e:
            return _error(402, "budget exceeded", accumulated_est_cost_usd=e.accumulated_cost_usd)
        except Exception as e:
            return _error(502, f"brief failed: {type(e).__name__}: {e}")
        record = {"day": day, "generated_at": datetime.now(timezone.utc).isoformat(), "verified": result.verified,
                  "attempts": result.attempts, "failed_checks": failed_labels(result.report),
                  "route": result.run.route, "model": result.run.model,
                  "est_cost_usd": result.total_est_cost_usd, "latency_ms": result.total_latency_ms}
        status.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        event("brief_generated", {k: record[k] for k in ("day", "verified", "attempts", "failed_checks")})
        return record

    @app.get("/brief/today", dependencies=auth)
    def brief_today():
        if briefs is None:
            return _error(409, "no brief data source configured (SENSEI_BRIEF_FIXTURE)")
        md, status = brief_paths(date.today().isoformat())
        if not md.is_file() or not status.is_file():
            return _error(404, "no brief yet today")
        return {**json.loads(status.read_text(encoding="utf-8")), "text": md.read_text(encoding="utf-8")}

    @app.get("/", include_in_schema=False)
    def root_redirect():
        return RedirectResponse("/ui", status_code=307)  # the bare address opens the UI

    @app.get("/ui", include_in_schema=False)
    def ui_shell():
        # Public on purpose: a token-entry page with no data. A browser cannot send
        # X-Sensei-Token on navigation; the page sends it on every request it makes.
        return FileResponse(UI_DIR / "index.html", media_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/ui/app/{name}", dependencies=auth, include_in_schema=False)
    def ui_app(name: str):
        if name not in UI_APP_FILES:
            return _error(404, "not found")
        return FileResponse(UI_DIR / name, media_type=UI_APP_FILES[name],
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.delete("/memory/{entry_id}", dependencies=auth)
    def delete(entry_id: str):
        try:
            memory_store.delete(entry_id)
        except KeyError:
            return _error(404, "memory entry not found")
        event("memory_deleted", {"entry_id": entry_id})
        return {"deleted": entry_id}

    return app


def main() -> None:
    """Serve a real local setup on 127.0.0.1: Ollama for local_private, fakes for API routes."""
    import uvicorn

    from sensei_core.clients import OllamaClient
    from sensei_core.config import load_models
    from sensei_core.router import FakeClient, Router

    home = Path(os.environ.get("SENSEI_HOME", Path.home() / ".sensei"))
    routes = load_models()
    local_model = os.environ.get("SENSEI_LOCAL_MODEL", routes["local_private"].model)
    routes["local_private"] = dataclasses.replace(routes["local_private"], model=local_model)

    trace = TraceWriter(home / "traces" / "gateway.jsonl", run_id="gateway", session_id="local")
    egress = EgressLog(home / "egress.jsonl")
    executor = Executor(
        Router(routes, trace=trace),
        {
            "local_private": OllamaClient.for_route(routes["local_private"]),
            "cheap_api": FakeClient("fake cheap_api response"),
            "frontier_api": FakeClient("fake frontier_api response"),
        },
        trace=trace,
        egress=egress,
    )
    fixture = os.environ.get("SENSEI_BRIEF_FIXTURE")
    try:
        app = create_app(executor, trace.path, MemoryStore(home / "memory"), trace,
                         brief_fixture=fixture, brief_dir=home / "briefs", egress_path=egress.path)
    except GatewayConfigError as e:
        raise SystemExit(f"Sensei gateway not started: {e}") from None
    host = bind_host()
    uvicorn.run(app, host=host, port=int(os.environ.get("SENSEI_GATEWAY_PORT", "8765")))


def bind_host(environ: dict[str, str] | None = None) -> str:
    """127.0.0.1 unless SENSEI_GATEWAY_ALLOW_LAN is exactly "1" (explicit opt-in, warned)."""
    env = os.environ if environ is None else environ
    if env.get(ALLOW_LAN_ENV) == "1":
        log.warning(
            "%s=1: binding 0.0.0.0 — the gateway is reachable from every device on this network, "
            "protected only by its token. Prefer Tailscale; unset this to return to localhost.", ALLOW_LAN_ENV)
        return "0.0.0.0"
    return "127.0.0.1"


if __name__ == "__main__":
    main()
