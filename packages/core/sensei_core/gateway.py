"""Gateway: Sensei's localhost HTTP API (DESIGN.md §2, Threat 7).

Token auth on every endpoint except /health, a kill switch that refuses
new runs, cost totals from the trace, and memory review/deletion.
All components are injected; main() wires a real local setup.

Run:  SENSEI_GATEWAY_TOKEN=... python -m sensei_core.gateway
"""

from __future__ import annotations

import dataclasses
import hmac
import os
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sensei_core.executor import BudgetExceeded, Executor, VerificationFailed
from sensei_core.ledger import CostLedger
from sensei_core.memory import MemoryStore
from sensei_core.trace import TraceWriter

TOKEN_ENV = "SENSEI_GATEWAY_TOKEN"


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

    @app.get("/costs", dependencies=auth)
    def costs() -> dict:
        if not ledger_path.exists():
            return {"basis": "estimated", "total_est_cost_usd": 0.0, "by_route": {}, "by_day": {}}
        ledger = CostLedger(ledger_path)
        return {
            "basis": "estimated",
            "total_est_cost_usd": ledger.total_est_cost_usd,
            "by_route": ledger.by_route(),
            "by_day": ledger.by_day(),
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
    executor = Executor(
        Router(routes, trace=trace),
        {
            "local_private": OllamaClient(local_model),
            "cheap_api": FakeClient("fake cheap_api response"),
            "frontier_api": FakeClient("fake frontier_api response"),
        },
        trace=trace,
    )
    try:
        app = create_app(executor, trace.path, MemoryStore(home / "memory"), trace)
    except GatewayConfigError as e:
        raise SystemExit(f"Sensei gateway not started: {e}") from None
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SENSEI_GATEWAY_PORT", "8765")))


if __name__ == "__main__":
    main()
