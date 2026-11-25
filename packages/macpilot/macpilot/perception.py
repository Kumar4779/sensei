"""Perception (MacPilot Slice 1): screenshot only.

The baseline sees the page the way a person does: a PNG of the viewport, and
nothing else. There is no accessibility tree and no DOM. AX/DOM perception is
a later challenger arm, measured against this baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from macpilot.coords import Viewport


@dataclass(frozen=True)
class Observation:
    screenshot: Path
    viewport: Viewport


def observe(page, path: str | Path) -> Observation:
    """Capture the viewport as a PNG at `path`; return it with the viewport size (CSS px)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out), type="png", full_page=False)
    size = page.viewport_size
    return Observation(out, Viewport(int(size["width"]), int(size["height"])))
