"""Normalized coordinates (MacPilot Slice 1).

The tool interface speaks in normalized points: x and y in [0, 1000], measured
across the browser viewport (CSS pixels), whatever its size. `ground` turns a
normalized point into viewport pixels for the browser. The coordinate space is
independent of the screenshot: a screenshot can be downscaled for a model
(or captured at a device scale factor of 2) without changing what (500, 500)
means, the centre of the viewport.
"""

from __future__ import annotations

from dataclasses import dataclass

SCALE = 1000


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("width", "height"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError(f"viewport {name} must be a positive int, got {v!r}")


@dataclass(frozen=True)
class NormPoint:
    x: int
    y: int

    def __post_init__(self) -> None:
        for name in ("x", "y"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= SCALE:
                raise ValueError(f"normalized {name} must be in [0, {SCALE}], got {v!r}")


def ground(point: NormPoint, viewport: Viewport) -> tuple[float, float]:
    """Normalized (0–1000) point -> viewport pixel coordinates (CSS px, sub-pixel allowed)."""
    return point.x / SCALE * viewport.width, point.y / SCALE * viewport.height


def normalize(px: float, py: float, viewport: Viewport) -> NormPoint:
    """Viewport pixels -> the nearest normalized point (the inverse of ground, to integer precision)."""
    if not (0 <= px <= viewport.width and 0 <= py <= viewport.height):
        raise ValueError(f"pixel ({px}, {py}) is outside the {viewport.width}x{viewport.height} viewport")
    return NormPoint(round(px / viewport.width * SCALE), round(py / viewport.height * SCALE))
