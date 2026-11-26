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
  trace_store    content stores (Slice 34; Threat 4), one check each: the traces
  run_store      directory, run records (which may hold model responses), the
  memory_store   memory store (entries, history, changelog, derived embeddings),
  brief_store    the briefs (inbox content) and egress.jsonl itself, not its
  egress_log     parent (the Sensei home, which the probe does not judge).
                 Readable or writable by any local user fails; by the group, warns;
                 owner only passes. A store not created yet is skipped, not passed:
                 the probe re-runs at every startup, so a store created loose later
                 is caught then.
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
STATUSES = ("pass", "warn", "fail", "skipped")
LABELS = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skipped": "SKIP"}
# check name -> (store name under the Sensei home, is a directory)
STORE_CHECKS = {
    "trace_store": ("traces", True),
    "run_store": ("runs", True),
    "memory_store": ("memory", True),
    "brief_store": ("briefs", True),
    "egress_log": ("egress.jsonl", False),
}
RELATIVE_PROBES = ("checker.py", "../checker.py", "../../checker.py", "../../../checker.py",
                   "../../evals/tasks", "../../../evals/tasks", "../../../../evals/tasks", "evals/results")


@dataclass(frozen=True)
class ProbeResult:
    check: str
    status: str  # pass | warn | fail | skipped
    evidence: str
    # Content stores only (Slice 34): the store's path, and the path and mode that decided the status.
    path: str | None = None
    decided_by: str | None = None
    mode: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class Posture:
    results: tuple[ProbeResult, ...]
    strict: bool
    probed_at: str

    @property
    def failed(self) -> list[ProbeResult]:
        return [r for r in self.results if r.status == "fail"]

    @property
    def stores(self) -> list[ProbeResult]:
        return [r for r in self.results if r.check in STORE_CHECKS]

    def to_dict(self) -> dict[str, Any]:
        counts = {s: sum(r.status == s for r in self.results) for s in STATUSES}
        return {"probed_at": self.probed_at, "strict": self.strict, "summary": counts,
                "results": [r.as_dict() for r in self.results],
                "stores": [{"store": r.check, "path": r.path, "status": r.status, "decided_by": r.decided_by,
                            "mode": r.mode, "evidence": r.evidence} for r in self.stores]}


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


# ---- e. content stores -----------------------------------------------------------------


def check_store(check: str, path: Path | None, *, directory: bool = True) -> ProbeResult:
    """One content store: a directory and everything under it, or one file (Slice 34)."""
    if path is None:
        return ProbeResult(check, "skipped", "not_configured: this process has no such store")
    path = Path(path)
    if not path.exists():
        return ProbeResult(check, "skipped", f"not_created: {path} does not exist yet; checked again at "
                                             "the next startup", str(path))
    world, group = [], []
    for p in [path, *sorted(path.rglob("*"))] if directory else [path]:
        mode = p.stat().st_mode
        if mode & (stat.S_IROTH | stat.S_IWOTH):
            world.append(p)
        elif mode & (stat.S_IRGRP | stat.S_IWGRP):
            group.append(p)
    fix = f"; fix: chmod {'-R ' if directory else ''}go-rwx {path}"

    def name(p: Path) -> str:
        return "the directory" if p == path and directory else str(p.relative_to(path if directory else path.parent))

    def result(status: str, found: list[Path], who: str, read_bit: int) -> ProbeResult:
        how = "readable" if found[0].stat().st_mode & read_bit else "writable"
        more = f" (+{len(found) - 1} more)" if len(found) > 1 else ""
        return ProbeResult(check, status, f"{path} {how} by {who}: {name(found[0])} {_mode(found[0])}{more}{fix}",
                           str(path), name(found[0]), _mode(found[0]))

    if world:
        return result("fail", world, "any local user", stat.S_IROTH)
    if group:
        return result("warn", group, "its group", stat.S_IRGRP)
    owned = "and its files: owner only" if directory else "owner only"
    return ProbeResult(check, "pass", f"{path} ({_mode(path)}) {owned}", str(path), name(path), _mode(path))


def check_trace_store(traces_dir: Path) -> ProbeResult:
    return check_store("trace_store", traces_dir)


def check_stores(home: Path, paths: dict[str, Path | None] | None = None) -> tuple[ProbeResult, ...]:
    """Every content store, at home unless paths overrides it (a gateway passes the stores it was given)."""
    paths = paths or {}
    return tuple(check_store(check, paths[check] if check in paths else home / name, directory=directory)
                 for check, (name, directory) in STORE_CHECKS.items())


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
              stores: dict[str, Path | None] | None = None, kill_switch: KillSwitch | None = None,
              routes: dict[str, Route] | None = None, clients: dict[str, object] | None = None,
              tasks_dir: Path = EVALS_TASKS, environ: dict[str, str] | None = None) -> Posture:
    results = (
        check_manifests(manifests),
        check_ground_truth(tasks_dir, environ),
        check_egress(routes, clients),
        check_kill_state(kill_switch or KillSwitch(home / KILL_FILE)),
        *check_stores(home, stores),
        check_token(token),
    )
    return Posture(results, strict, datetime.now(timezone.utc).isoformat())


def format_posture(posture: Posture) -> str:
    mode = "strict: any fail refuses startup" if posture.strict else "default: posture is information"
    counts = posture.to_dict()["summary"]
    lines = [f"Sensei containment probe ({mode})"]
    lines += [f"  {LABELS[r.status]:<4}  {r.check:<12}  {r.evidence}" for r in posture.results]
    lines.append(f"  summary: {counts['pass']} pass, {counts['warn']} warn, {counts['fail']} fail, "
                 f"{counts['skipped']} skipped")
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
