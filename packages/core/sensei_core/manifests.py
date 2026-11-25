"""Guardrail manifest integrity (Slice 24): the agent cannot rewrite its own rules.

tools.toml (PermissionGate) and models.toml (Router) are the data that
constrain Sensei. They are only as strong as their integrity check, so:

- A ManifestGuard pins the SHA-256 digest of each manifest when it is built
  (Executor construction, which the Gateway's startup goes through) and traces
  "manifests_loaded" with the digests, never the contents.
- verify() re-reads both files before every run. Any change (edited, deleted)
  traces "manifest_tamper_detected" naming the file(s) and raises
  ManifestTamperError: the run is refused.
- Pins are process-wide. Once a manifest path is pinned, load_models() and
  load_tools() refuse to parse it if its bytes no longer match, and a new
  guard over a changed file refuses too. So no component can reload a changed
  manifest within the same process: there is no silent reload path. To adopt
  an intended edit, restart the process.
- A group- or world-writable manifest gets a traced "manifest_permissions_loose"
  warning at startup (v1: warn, do not refuse; this is a dev machine).
"""

from __future__ import annotations

import hashlib
import stat
import threading
from pathlib import Path

from sensei_core.trace import TraceWriter

MODELS_MANIFEST = Path(__file__).with_name("models.toml")
TOOLS_MANIFEST = Path(__file__).with_name("tools.toml")

_pins: dict[Path, str] = {}  # resolved path -> digest pinned in this process
_lock = threading.Lock()


class ManifestTamperError(RuntimeError):
    """A guardrail manifest changed after it was loaded; the run is refused."""

    def __init__(self, changed: list[str]) -> None:
        self.changed = changed
        super().__init__(f"manifest_tamper_detected: {', '.join(changed)} changed since startup")


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_file(path: Path) -> str | None:
    try:
        return digest_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def check_unchanged(path: str | Path, data: bytes) -> None:
    """Called by the manifest loaders: refuse to parse a pinned manifest whose bytes changed."""
    resolved = Path(path).resolve()
    with _lock:
        pinned = _pins.get(resolved)
    if pinned is not None and digest_bytes(data) != pinned:
        raise ManifestTamperError([resolved.name])


class ManifestGuard:
    def __init__(self, paths: dict[str, str | Path] | None = None, trace: TraceWriter | None = None) -> None:
        self.paths = {name: Path(p).resolve() for name, p in (paths or default_paths()).items()}
        self.trace = trace
        digests: dict[str, str] = {}
        changed: list[str] = []
        with _lock:
            for name, path in self.paths.items():
                current = _digest_file(path)
                if current is None:
                    raise FileNotFoundError(f"guardrail manifest not found: {path}")
                pinned = _pins.setdefault(path, current)
                if pinned != current:  # changed since another component pinned it
                    changed.append(name)
                digests[name] = pinned
        self.digests = digests
        if changed:
            self._tamper(changed)
        self._event("manifests_loaded", dict(self.digests))
        self._check_permissions()

    def _event(self, event_type: str, payload: dict) -> None:
        if self.trace is not None:
            self.trace.write(event_type, payload)

    def _tamper(self, changed: list[str]) -> None:
        self._event("manifest_tamper_detected", {"changed": changed})
        raise ManifestTamperError(changed)

    def _check_permissions(self) -> None:
        for name, path in self.paths.items():
            mode = path.stat().st_mode
            loose = [w for w, bit in (("group", stat.S_IWGRP), ("world", stat.S_IWOTH)) if mode & bit]
            if loose:
                self._event("manifest_permissions_loose",
                            {"file": name, "mode": oct(stat.S_IMODE(mode)), "writable_by": loose})

    def verify(self) -> None:
        """Re-read every manifest; raise ManifestTamperError (traced) if any changed."""
        changed = [name for name, path in self.paths.items() if _digest_file(path) != self.digests[name]]
        if changed:
            self._tamper(changed)


def default_paths() -> dict[str, Path]:
    return {"tools.toml": TOOLS_MANIFEST, "models.toml": MODELS_MANIFEST}
