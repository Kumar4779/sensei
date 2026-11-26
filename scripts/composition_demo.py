"""Slice 25: the composition demo. Sensei's modules working as one product, in one traced run.

    packages/core/.venv/bin/python scripts/composition_demo.py [--port 8765] [--wait-minutes 15]

Everything goes through the real components, in order:
  1. start the gateway (scratch Sensei home, fresh token)
  2. propose a memory entry through the Custodian MCP server (stdio, real SDK client)
  3. POST /brief/run on the morning-brief-001 fixture; it must come back verified
  4. the MacPilot leg: run MacPilot's own Slice 1 demo task (python -m macpilot.demo, in its
     own virtualenv); it counts only if MacPilot's audit log shows goal_reached
  5. publish the UI to the tailnet (tailscale serve), print the phone URL, and WAIT until
     the gateway's trace shows memory_confirmed for the proposed entry
  6. kill-switch test: /kill -> /run must be 503 -> /revive
  7. print a summary in which every claim cites the trace event that proves it;
     anything without its event is reported NOT DONE

Nothing here changes module behaviour; the script only drives the gateway's and the
Custodian's public interfaces and reads their traces. The scratch home is kept for
inspection; the gateway is stopped and any tailscale serve config this script added
is withdrawn on exit.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "packages" / "core"
FIXTURE = REPO / "evals" / "tasks" / "morning-brief-001" / "fixture"
MACPILOT = REPO / "packages" / "macpilot"
DEMO_FACT = "The user prefers meetings after 10:00, never before."


# ---- evidence: trace events and access-log lines ---------------------------------------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def find_event(events: list[dict], event_type: str, after: str = "", **payload: Any) -> dict | None:
    """First event of a type (at or after an ISO timestamp) whose payload includes the given items."""
    for e in events:
        if e.get("event_type") != event_type or e.get("timestamp", "") < after:
            continue
        if all(e.get("payload", {}).get(k) == v for k, v in payload.items()):
            return e
    return None


@dataclass
class Claim:
    text: str
    evidence: list[str] = field(default_factory=list)  # citations; empty means NOT DONE
    detail: str = ""

    @property
    def done(self) -> bool:
        return bool(self.evidence)


def cite(event: dict | None, source: str) -> list[str]:
    if event is None:
        return []
    payload = json.dumps(event.get("payload", {}), sort_keys=True)
    return [f"{source}: {event['event_type']} @ {event['timestamp']} {payload}"]


def format_summary(claims: list[Claim]) -> str:
    lines = ["", "=" * 72, "SENSEI COMPOSITION DEMO — SUMMARY (every claim cites its evidence)", "=" * 72]
    for c in claims:
        lines.append(f"[{'DONE' if c.done else 'NOT DONE'}] {c.text}")
        if c.detail:
            lines.append(f"    {c.detail}")
        for ev in c.evidence:
            lines.append(f"    evidence: {ev}")
        if not c.done:
            lines.append("    evidence: none (no trace event proves it)")
    done = sum(c.done for c in claims)
    lines += ["-" * 72, f"{done}/{len(claims)} claims proven by the trace.", ""]
    return "\n".join(lines)


# ---- MacPilot: run its own demo task, never fake a result ------------------------------


def macpilot_run(out_dir: Path, data_dir: Path, root: Path = MACPILOT) -> tuple[int, str]:
    """Run MacPilot's Slice 1 demo task through its own CLI and virtualenv (it needs Playwright,
    which the core venv does not have). Returns (exit code, output)."""
    python = root / ".venv" / "bin" / "python"
    if not python.is_file():
        return -1, f"{python} not found; set up packages/macpilot (see its README)"
    r = subprocess.run([str(python), "-m", "macpilot.demo", "--out", str(out_dir), "--data-dir", str(data_dir)],
                       cwd=root, capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def macpilot_goal_lines(audit: Path) -> list[str]:
    """MacPilot's own audit-log lines recording goal_reached: the only evidence the leg counts."""
    if not audit.is_file():
        return []
    return [ln.strip() for ln in audit.read_text(encoding="utf-8").splitlines()
            if ln.strip() and json.loads(ln).get("outcome") == "goal_reached"]


# ---- talking to the gateway -------------------------------------------------------------


class Gateway:
    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base, token

    def call(self, method: str, path: str, body: dict | None = None, auth: bool = True) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if auth:
            headers["X-Sensei-Token"] = self.token
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


def wait_for_health(gw: Gateway, seconds: float = 30) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if gw.call("GET", "/health", auth=False)[0] == 200:
                return
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(0.5)
    raise SystemExit("gateway did not become healthy")


def custodian_propose(home: Path, fact: str) -> dict[str, Any]:
    """Propose through the real Custodian MCP server over stdio, as an MCP client would."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import Implementation

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sensei_core.custodian_server", "serve", "--root", str(home / "memory"),
              "--trace", str(home / "traces" / "custodian.jsonl")],
    )

    async def go() -> dict[str, Any]:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w, client_info=Implementation(name="composition-demo", version="1")) as s:
                await s.initialize()
                res = await s.call_tool("memory_propose", {"text": fact})
                return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)

    return anyio.run(go)


# ---- Tailscale (the README's phone path) ---------------------------------------------------


def tailscale_publish(port: int) -> tuple[str | None, bool, str | None, str]:
    """(phone URL, whether this script added the serve config, phone tailnet IP, note)."""
    ts = shutil.which("tailscale")
    if ts is None:
        return None, False, None, "tailscale CLI not found; phone step cannot run"
    status = subprocess.run([ts, "serve", "status"], capture_output=True, text=True).stdout
    added = False
    if "No serve config" in status or not status.strip():
        subprocess.run([ts, "serve", "--bg", str(port)], capture_output=True, text=True, timeout=120, check=True)
        added = True
    me = json.loads(subprocess.run([ts, "status", "--json"], capture_output=True, text=True, check=True).stdout)
    dns = me.get("Self", {}).get("DNSName", "").rstrip(".")
    phone_ip = next((p["TailscaleIPs"][0] for p in me.get("Peer", {}).values() if p.get("OS") == "iOS"), None)
    return (f"https://{dns}/" if dns else None), added, phone_ip, "published to the tailnet only"


def phone_access_lines(log: Path, phone_ip: str | None, path: str) -> list[str]:
    if not phone_ip or not log.is_file():
        return []
    return [ln.strip() for ln in log.read_text(encoding="utf-8").splitlines() if f"{phone_ip}:" in ln and f" {path} " in ln]


# ---- the run ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--wait-minutes", type=float, default=15.0, help="how long to wait for the phone approval")
    args = ap.parse_args(argv)

    home = Path(tempfile.mkdtemp(prefix="sensei-demo-"))
    token = secrets.token_urlsafe(24)
    gw = Gateway(f"http://127.0.0.1:{args.port}", token)
    trace_path = home / "traces" / "gateway.jsonl"
    access_log = home / "gateway-access.log"
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    print(f"scratch Sensei home: {home}")

    env = {**os.environ, "SENSEI_GATEWAY_TOKEN": token, "SENSEI_HOME": str(home),
           "SENSEI_BRIEF_FIXTURE": str(FIXTURE), "SENSEI_GATEWAY_PORT": str(args.port)}
    env.pop("SENSEI_GATEWAY_ALLOW_LAN", None)  # localhost only; the phone path is Tailscale
    log_f = access_log.open("w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "sensei_core.gateway"], cwd=CORE, env=env,
                            stdout=log_f, stderr=subprocess.STDOUT)
    ts_added = False
    notes: dict[str, str] = {}
    try:
        wait_for_health(gw)
        print(f"1. gateway up on 127.0.0.1:{args.port} (pid {proc.pid})")

        entry = custodian_propose(home, DEMO_FACT)
        print(f"2. Custodian proposed {entry['id']} ({entry['status']}, origin {entry['origin']})")

        status, brief = gw.call("POST", "/brief/run")
        print(f"3. /brief/run -> HTTP {status}, verified={brief.get('verified') if isinstance(brief, dict) else None}")

        mp_out = home / "macpilot"
        mp_code, mp_output = macpilot_run(mp_out, home / "macpilot-data")
        last = mp_output.strip().splitlines()[-1] if mp_output.strip() else "(no output)"
        notes["macpilot"] = f"python -m macpilot.demo exited {mp_code}: {last}"
        print(f"4. MacPilot leg: {notes['macpilot']}")

        url, ts_added, phone_ip, ts_note = tailscale_publish(args.port)
        notes["tailscale"] = ts_note
        if url is None:
            print(f"5. phone step skipped: {ts_note}")
        else:
            print("5. On the iPhone (Tailscale connected), open:\n"
                  f"     {url}\n   token: {token}\n"
                  f"   then Confirm \"{DEMO_FACT}\" under Approvals.\n"
                  f"   waiting up to {args.wait_minutes:g} min for memory_confirmed in the trace...", flush=True)
            deadline = time.time() + args.wait_minutes * 60
            while time.time() < deadline and not find_event(read_jsonl(trace_path), "memory_confirmed", entry_id=entry["id"]):
                time.sleep(2)

        gw.call("POST", "/kill")
        run_status, run_body = gw.call("POST", "/run", {"prompt": "hello", "sensitivity_class": "LOCAL_ONLY",
                                                        "difficulty": "simple"})
        gw.call("POST", "/revive")
        print(f"6. kill switch: /run while killed -> HTTP {run_status} {run_body}")
        _, costs = gw.call("GET", "/costs")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log_f.close()
        if ts_added:
            subprocess.run([shutil.which("tailscale") or "tailscale", "serve", "--https=443", "off"], capture_output=True)

    # ---- the summary: built only from the records ----
    events = read_jsonl(trace_path)
    custodian = read_jsonl(home / "traces" / "custodian.jsonl")
    since = started_at
    claims: list[Claim] = []

    prop = find_event(custodian, "mcp_tool_call", operation="memory_propose", entry_id=entry["id"])
    claims.append(Claim("Memory entry proposed through the Custodian MCP server (pending, never active)",
                        cite(prop, "custodian trace"), f"entry {entry['id']}: {DEMO_FACT!r}"))

    brief_ev = find_event(events, "brief_generated", verified=True)
    claims.append(Claim("Morning brief generated and verified (morning-brief-001 fixture)",
                        cite(brief_ev, "gateway trace")))

    mp_audit = mp_out / "audit.jsonl"
    claims.append(Claim("MacPilot leg: grounded click on the fixture page, goal state reached",
                        [f"macpilot audit log {mp_audit}: {l}" for l in macpilot_goal_lines(mp_audit)]
                        if mp_code == 0 else [], notes["macpilot"]))

    confirmed = find_event(events, "memory_confirmed", entry_id=entry["id"])
    phone_lines = phone_access_lines(access_log, phone_ip if url else None, "/memory/confirm")
    claims.append(Claim("Memory entry confirmed from the iPhone UI",
                        cite(confirmed, "gateway trace") + [f"access log (iPhone {phone_ip}): {l}" for l in phone_lines]
                        if confirmed and phone_lines else [],
                        "requires BOTH memory_confirmed and the iPhone's tailnet IP on POST /memory/confirm"
                        + ("" if confirmed else "; memory_confirmed not in trace")
                        + ("" if phone_lines or not confirmed else "; confirm did not come from the iPhone's IP")))

    egress = (costs or {}).get("egress", {})
    by_dest = egress.get("by_destination", {})
    egress_file = home / "egress.jsonl"
    claims.append(Claim(f"Today's egress: {sum(d['calls'] for d in by_dest.values())} API calls, "
                        f"{sum(d['total_bytes'] for d in by_dest.values())} bytes",
                        [f"GET /costs egress = {json.dumps(egress, sort_keys=True)}; "
                         f"egress.jsonl {'has ' + str(len(read_jsonl(egress_file))) + ' lines' if egress_file.exists() else 'absent'}"],
                        "every model call in this demo was LOCAL_ONLY (local_private), so nothing left the machine"
                        if not by_dest else ""))

    engaged = find_event(events, "kill_switch_engaged", after=since)
    refused = find_event(events, "gateway_run_refused", after=engaged["timestamp"] if engaged else "~")
    revived = find_event(events, "kill_switch_disengaged", after=refused["timestamp"] if refused else "~")
    claims.append(Claim("Kill switch: engage -> /run refused (503) -> revive",
                        (cite(engaged, "gateway trace") + cite(refused, "gateway trace") + cite(revived, "gateway trace"))
                        if engaged and refused and revived and run_status == 503 else [],
                        f"/run while killed returned HTTP {run_status}"))

    print(format_summary(claims))
    print(f"records kept in {home} (gateway trace, custodian trace, access log)")
    return 0 if all(c.done for c in claims) else 1


if __name__ == "__main__":
    sys.exit(main())
