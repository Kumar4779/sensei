"""The dedicated, disposable browser profile (MacPilot Slice 1).

MacPilot never touches the user's real browser profile (DESIGN.md §2). It runs
Playwright's own Chromium with a persistent profile under the MacPilot data
directory:

    $MACPILOT_DATA_DIR/chromium-profile      (default: ~/.sensei/macpilot/chromium-profile)

The profile is disposable: `reset_profile()` deletes it, and the next launch
starts from a clean one. A profile path inside a real browser's own data
directory is refused.
"""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from macpilot.coords import Viewport

DEFAULT_VIEWPORT = Viewport(1280, 720)
# Real browser profile locations MacPilot must never use (macOS and Linux).
_REAL_PROFILE_ROOTS = (
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    Path.home() / "Library" / "Application Support" / "Chromium",
    Path.home() / "Library" / "Application Support" / "BraveSoftware",
    Path.home() / "Library" / "Application Support" / "Microsoft Edge",
    Path.home() / ".config" / "google-chrome",
    Path.home() / ".config" / "chromium",
)


def data_dir() -> Path:
    return Path(os.environ.get("MACPILOT_DATA_DIR") or Path.home() / ".sensei" / "macpilot").expanduser()


def profile_dir(root: str | Path | None = None) -> Path:
    path = (Path(root) if root is not None else data_dir()) / "chromium-profile"
    resolved = path.expanduser().resolve()
    for real in _REAL_PROFILE_ROOTS:
        if resolved == real.resolve() or real.resolve() in resolved.parents:
            raise ValueError(f"refusing to use a real browser profile: {resolved}")
    return resolved


def reset_profile(root: str | Path | None = None) -> Path:
    """Delete the disposable profile; the next launch starts clean."""
    path = profile_dir(root)
    shutil.rmtree(path, ignore_errors=True)
    return path


@contextmanager
def launch(root: str | Path | None = None, *, headless: bool = True,
           viewport: Viewport = DEFAULT_VIEWPORT) -> Iterator[object]:
    """Yield a Playwright Page in the disposable profile; close everything afterwards."""
    from playwright.sync_api import sync_playwright

    path = profile_dir(root)
    path.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(path), headless=headless,
            viewport={"width": viewport.width, "height": viewport.height},
            accept_downloads=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            context.close()
