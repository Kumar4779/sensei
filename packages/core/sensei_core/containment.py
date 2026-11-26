"""Containment self-probe (Slice 32): Sensei tests its own posture instead of claiming it.

    python -m sensei_core.containment [--strict]

Run at gateway startup and on demand. Each check returns pass, warn or fail
with a one-line evidence string. The probe states only what it observed. Where
a defence is not built, it says so.

  manifests      tools.toml and models.toml are not writable by group or other
                 (world-writable fails, group-writable warns).
  ground_truth   no SenseiBench checker is reachable from a trial sandbox. The
                 Slice 23 hostile probe is re-run against fresh sandboxes built by
                 the bench's own prepare_trial: the sandbox tree, the working
                 directory's parents, known relative paths, and the environment.
  egress         stated, never tested by sending: which API routes are
                 configured, and that egress is LOGGED, NOT ENFORCED. That
                 always warns until enforcement exists.
  kill_state     the file-backed kill switch, as it reads now.
  trace_store    the traces directory and its files are not readable by group or
                 other (world-readable fails, group-readable warns; Threat 4).
  token          SENSEI_GATEWAY_TOKEN is set and at least 16 characters. Only
                 the length is reported, never the value.

Default mode reports and startup proceeds: on a dev machine, posture is
information. Strict mode (--strict, or SENSEI_STRICT_POSTURE=1) refuses
startup on any fail; a warn is allowed. Manifest tamper is still governed by
Slice 24, unchanged.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sensei_core.config import Route, load_models
from sensei_core.egress import API_TIERS
from sensei_core.killswitch import KILL_FILE, KillSwitch
from sensei_core.manifests import default_paths

STRICT_ENV = "SENSEI_STRICT_POSTURE"
TOKEN_ENV = "SENSEI_GATEWAY_TOKEN"
MIN_TOKEN_CHARS = 16
EVALS_TASKS = Path(__file__).resolve().parents[3] / "evals" / "tasks"
# Pinned by a test: this check may not start claiming enforcement until enforcement exists.
EGRESS_NOT_ENFORCED = "egress enforcement not built: egress is LOGGED, NOT ENFORCED"
RELATIVE_PROBES = ("checker.py", "../checker.py", "../../checker.py", "../../../checker.py",
                   "../../evals/tasks", "../../../evals/tasks", "../../../../evals/tasks", "evals/results")


@dataclass(frozen=True)
class ProbeResult:
    check: str
    status: str  # pass | warn | fail
    evidence: str


@dataclass(frozen=True)
class Posture:
    results: tuple[ProbeResult, ...]
    strict: bool
    probed_at: str

    @property
    def failed(self) -> list[ProbeResult]:
        return [r for r in self.results if r.status == "fail"]

    def to_dict(self) -> dict[str, Any]:
        counts = {s: sum(r.status == s for r in self.results) for s in ("pass", "warn", "fail")}
        return {"probed_at": self.probed_at, "strict": self.strict, "summary": counts,
                "results": [asdict(r) for r in self.results]}


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[2:]


# ---- a. manifests -----------------------------------------------------------------------


def check_manifests(paths: dict[str, Path] | None = None) -> ProbeResult:
    paths = paths or default_paths()
    world, group, seen = [], [], []
    for name, path in sorted(paths.items()):
        if not path.is_file():
            return ProbeResult("manifests", "fail", f"{name} missing at {path}")
        mode = path.stat().st_mode
        seen.append(f"{name} {_mode(path)}")
        if mode & stat.S_IWOTH:
            world.append(name)
        elif mode & stat.S_IWGRP:
            group.append(name)
    listing = ", ".join(seen)
    if world:
        return ProbeResult("manifests", "fail", f"{listing}: {', '.join(world)} writable by any local user")
    if group:
        return ProbeResult("manifests", "warn", f"{listing}: {', '.join(group)} writable by the file's group")
    return ProbeResult("manifests", "pass", f"{listing}: not writable by group or other")


# ---- b. ground truth --------------------------------------------------------------------


def _sandbox_leaks(task, sandbox: Path, work: Path, environ: dict[str, str]) -> list[str]:
    """The Slice 23 hostile probe, resolved against the sandbox (no chdir: the gateway is threaded)."""
    leaks = []
    for p in sandbox.rglob("*"):  # everything a trial is handed is reachable by design
        if p.name in ("checker.py", "task.json") or p.name == "results":
            leaks.append(f"in the sandbox: {p.relative_to(sandbox)}")
        elif p.is_file() and filecmp.cmp(p, task.checker, shallow=False):
            leaks.append(f"a copy of the checker in the sandbox: {p.relative_to(sandbox)}")
    for parent in work.parents:
        if (parent / "checker.py").exists() or (parent / "task.json").exists():
            leaks.append(f"in a parent of the working directory: {parent}")
    for rel in RELATIVE_PROBES:
        if (work / rel).exists():
            leaks.append(f"relative path resolves: {rel}")
    material = [str(task.checker), str(task.task_dir), str(task.task_dir.parents[1] / "results")]
    for key, value in environ.items():
        if any(m in value for m in material):
            leaks.append(f"env {key} names grading material")
    return leaks


def check_ground_truth(tasks_dir: Path = EVALS_TASKS, environ: dict[str, str] | None = None) -> ProbeResult:
    from sensei_core.bench import load_task, prepare_trial

    environ = dict(os.environ if environ is None else environ)
    task_dirs = sorted(p.parent for p in tasks_dir.glob("*/task.json")) if tasks_dir.is_dir() else []
    if not task_dirs:
        return ProbeResult("ground_truth", "warn", f"no SenseiBench tasks at {tasks_dir}; nothing probed")
    leaks: list[str] = []
    for task_dir in task_dirs:
        task = load_task(task_dir)
        with tempfile.TemporaryDirectory(prefix="senseibench-probe-") as root:
            inp = prepare_trial(task, Path(root) / "trial")
            leaks += [f"{task.id}: {leak}" for leak in _sandbox_leaks(task, Path(root), inp.work_dir, environ)]
    if leaks:
        more = f" (+{len(leaks) - 1} more)" if len(leaks) > 1 else ""
        return ProbeResult("ground_truth", "fail", f"checker reachable from a trial sandbox: {leaks[0]}{more}")
    return ProbeResult("ground_truth", "pass",
                       f"{len(task_dirs)} task(s): no checker reachable from a trial sandbox (sandbox tree, "
                       "parents, relative paths, env); isolation by construction, not an OS sandbox")


# ---- c. egress --------------------------------------------------------------------------


def check_egress(routes: dict[str, Route] | None = None, clients: dict[str, object] | None = None) -> ProbeResult:
    routes = routes if routes is not None else load_models()
    api = sorted(n for n, r in routes.items() if r.tier in API_TIERS)
    if not api:
        configured = "no API route configured"
    elif clients is None:
        configured = f"API routes configured: {', '.join(api)} (clients not inspected)"
    else:
        from sensei_core.router import FakeClient

        def kind(name: str) -> str:
            client = clients.get(name)
            return "no client" if client is None else "fake client" if isinstance(client, FakeClient) else "real client"

        configured = f"API routes configured: {', '.join(f'{n} ({kind(n)})' for n in api)}"
    return ProbeResult("egress", "warn", f"{EGRESS_NOT_ENFORCED}; {configured}")


# ---- d. kill state ----------------------------------------------------------------------


def check_kill_state(switch: KillSwitch) -> ProbeResult:
    present = switch.path.exists()
    engaged = switch.engaged()  # fails closed and traces kill_state_unreadable on a bad file
    if present and not engaged:  # unreachable while engaged() fails closed; checked, never assumed
        return ProbeResult("kill_state", "fail", f"{switch.path} exists but is not honoured")
    if engaged:
        return ProbeResult("kill_state", "warn", f"ENGAGED: {switch.path} present and honoured; "
                                                 "every run is refused until POST /revive")
    return ProbeResult("kill_state", "pass", f"not engaged: no {switch.path.name} at {switch.path.parent}")


# ---- e. trace store ---------------------------------------------------------------------


def check_trace_store(traces_dir: Path) -> ProbeResult:
    if not traces_dir.exists():
        return ProbeResult("trace_store", "pass", f"{traces_dir} does not exist yet; nothing to read")
    world, group = [], []
    for p in [traces_dir, *sorted(traces_dir.rglob("*"))]:
        mode = p.stat().st_mode
        name = "the directory" if p == traces_dir else p.name
        if mode & stat.S_IROTH:
            world.append(f"{name} {_mode(p)}")
        elif mode & stat.S_IRGRP:
            group.append(f"{name} {_mode(p)}")
    fix = f"; fix: chmod -R go-rwx {traces_dir}"
    if world:
        more = f" (+{len(world) - 1} more)" if len(world) > 1 else ""
        return ProbeResult("trace_store", "fail", f"{traces_dir} readable by any local user: {world[0]}{more}{fix}")
    if group:
        more = f" (+{len(group) - 1} more)" if len(group) > 1 else ""
        return ProbeResult("trace_store", "warn", f"{traces_dir} readable by its group: {group[0]}{more}{fix}")
    return ProbeResult("trace_store", "pass", f"{traces_dir} ({_mode(traces_dir)}) and its files: owner only")


# ---- f. token ---------------------------------------------------------------------------


def check_token(token: str | None) -> ProbeResult:
    if not token:
        return ProbeResult("token", "fail", f"{TOKEN_ENV} is not set")
    if len(token) < MIN_TOKEN_CHARS:
        return ProbeResult("token", "fail", f"{TOKEN_ENV} is {len(token)} characters; at least {MIN_TOKEN_CHARS} required")
    return ProbeResult("token", "pass", f"{TOKEN_ENV} set, {len(token)} characters")


# ---- the probe --------------------------------------------------------------------------


def strict_from_env(environ: dict[str, str] | None = None) -> bool:
    return (os.environ if environ is None else environ).get(STRICT_ENV) == "1"


def run_probe(*, home: Path, token: str | None, strict: bool = False, manifests: dict[str, Path] | None = None,
              traces_dir: Path | None = None, kill_switch: KillSwitch | None = None,
              routes: dict[str, Route] | None = None, clients: dict[str, object] | None = None,
              tasks_dir: Path = EVALS_TASKS, environ: dict[str, str] | None = None) -> Posture:
    results = (
        check_manifests(manifests),
        check_ground_truth(tasks_dir, environ),
        check_egress(routes, clients),
        check_kill_state(kill_switch or KillSwitch(home / KILL_FILE)),
        check_trace_store(traces_dir or home / "traces"),
        check_token(token),
    )
    return Posture(results, strict, datetime.now(timezone.utc).isoformat())


def format_posture(posture: Posture) -> str:
    mode = "strict: any fail refuses startup" if posture.strict else "default: posture is information"
    counts = posture.to_dict()["summary"]
    lines = [f"Sensei containment probe ({mode})"]
    lines += [f"  {r.status.upper():<4}  {r.check:<12}  {r.evidence}" for r in posture.results]
    lines.append(f"  summary: {counts['pass']} pass, {counts['warn']} warn, {counts['fail']} fail")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sensei_core.containment", description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true", help=f"exit 1 on any fail (also {STRICT_ENV}=1)")
    args = ap.parse_args(argv)
    strict = args.strict or strict_from_env()
    home = Path(os.environ.get("SENSEI_HOME", Path.home() / ".sensei"))
    posture = run_probe(home=home, token=os.environ.get(TOKEN_ENV), strict=strict)
    print(format_posture(posture))
    return 1 if strict and posture.failed else 0


if __name__ == "__main__":
    sys.exit(main())
