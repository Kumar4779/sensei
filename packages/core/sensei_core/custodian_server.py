"""Custodian MCP server (Slice 21): the Custodian's four operations over stdio.

    python -m sensei_core.custodian_server serve   [--root DIR] [--trace FILE]
    python -m sensei_core.custodian_server confirm ID [--root DIR] [--trace FILE]
    python -m sensei_core.custodian_server pending [--root DIR]

`serve` speaks MCP over stdio only: no network listener of any kind, so the
server stays inside the local trust boundary. It exposes exactly four tools
(memory_search, memory_get, memory_propose, memory_list_pending) and no way to
activate an entry. `confirm` is the Sensei-side activation, run by the user on
this machine; it is deliberately not an MCP tool.

Defaults: --root $SENSEI_MEMORY_ROOT or ~/.sensei/memory; --trace
<root>/../custodian-trace.jsonl (outside the store root, so entry deletion
stays provable).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from sensei_core.config import load_embeddings
from sensei_core.custodian import Custodian
from sensei_core.memory import CATEGORIES, MemoryStore
from sensei_core.trace import TraceWriter

TOOL_NAMES = ("memory_search", "memory_get", "memory_propose", "memory_list_pending")


def client_name(ctx: Context | None) -> str:
    """The calling client's self-reported name from MCP initialize (clientInfo.name)."""
    try:
        params = ctx.session.client_params  # type: ignore[union-attr]
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        name = getattr(info, "name", None)
    except Exception:
        name = None
    return name if isinstance(name, str) and name.strip() else "unknown-client"


def build_server(custodian: Custodian) -> MCPServer:
    server = MCPServer(
        name="sensei-custodian",
        instructions=(
            "Sensei's shared memory. Search and read active entries; propose new facts. "
            "Proposals are never active until the user confirms them in Sensei."
        ),
    )

    @server.tool(description="Concept search over Sensei's ACTIVE memory entries. Returns entry ids and short "
                             "summaries, not raw files. mode says whether embeddings or the keyword fallback was used.")
    def memory_search(query: str, ctx: Context) -> dict[str, Any]:
        return custodian.search(query, client=client_name(ctx))

    @server.tool(description="One memory entry in full, including provenance (source, origin, status, timestamps).")
    def memory_get(id: str, ctx: Context) -> dict[str, Any]:
        return custodian.get(id, client=client_name(ctx))

    @server.tool(description="Propose a NEW fact for Sensei's memory. It is stored as pending (source=tool_content, "
                             f"origin names this client) and is never active until the user confirms it in Sensei. "
                             f"category: one of {sorted(CATEGORIES)}. A near-duplicate of an active entry is still "
                             "stored, flagged possible_duplicate_of in its origin.")
    def memory_propose(text: str, ctx: Context, category: str = "general") -> dict[str, Any]:
        return custodian.propose(text, category, client=client_name(ctx))

    @server.tool(description="Memory entries awaiting the user's confirmation (proposed or pending_confirmation).")
    def memory_list_pending(ctx: Context) -> dict[str, Any]:
        return {"pending": custodian.list_pending(client=client_name(ctx))}

    return server


def default_root() -> Path:
    return Path(os.environ.get("SENSEI_MEMORY_ROOT") or Path.home() / ".sensei" / "memory").expanduser()


def _trace_for(args: argparse.Namespace, root: Path) -> TraceWriter:
    path = Path(args.trace) if args.trace else root.parent / "custodian-trace.jsonl"
    return TraceWriter(path, run_id=f"custodian-{uuid.uuid4().hex[:8]}", session_id=f"custodian-{os.getpid()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sensei_core.custodian_server")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "confirm", "pending"):
        p = sub.add_parser(name)
        p.add_argument("--root", help="memory store root (default: $SENSEI_MEMORY_ROOT or ~/.sensei/memory)")
        p.add_argument("--trace", help="trace file (default: <root>/../custodian-trace.jsonl)")
        if name == "confirm":
            p.add_argument("entry_id")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser() if args.root else default_root()
    trace = _trace_for(args, root)

    if args.command == "serve":
        build_server(Custodian.for_root(root, load_embeddings(), trace=trace)).run("stdio")
        return 0
    store = MemoryStore(root)
    if args.command == "pending":
        for e in store.list_pending():
            print(f"{e['id']}  {e['status']:20s}  {e['category']:15s}  {e['origin']}")
        return 0
    entry = store.confirm(args.entry_id)  # Sensei-side activation, Slice 6 semantics
    trace.write("memory_confirm", {"client": "sensei-cli", "operation": "confirm", "entry_id": entry["id"],
                                   "status": entry["status"]})
    print(json.dumps({"id": entry["id"], "status": entry["status"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
