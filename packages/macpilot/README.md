# MacPilot

Sensei's hands: verified computer use on the Mac, starting with the browser.

**Status: in progress (Slice 1).** MacPilot can run the smallest verifiable
browser loop: a screenshot, one click at a normalized point, a deterministic
check of the end state, and an audit log of the step. It has no planner, no
model, no verifier beyond that end-state check, and no permission gate yet.
The click coordinates in the demo are given, not inferred. It cannot yet
perform any of the 20 benchmark tasks in
[`evals/macpilot/tasks.md`](../../evals/macpilot/tasks.md).

## Place in the architecture

MacPilot is a tool in the [DESIGN.md](../../DESIGN.md) §2 Tools layer
(browser and desktop), reached by the Agent Runtime through the same
permission tiers as every other tool (from a later slice).

- **A dedicated browser profile, never the user's real one** (see below).
- **Screenshot-only perception is the baseline.** No accessibility tree and no
  DOM. AX/DOM perception is a later challenger arm, measured against this
  baseline (the DESIGN.md §4 perception ablation).
- **Normalized coordinates.** The tool interface uses points with x and y in
  0–1000 across the viewport. They are grounded to viewport pixels at
  click time, so they don't depend on the screenshot's resolution: a
  downscaled or 2x screenshot does not change what (500, 500) means.
- **Every action carries a stop condition phrased as the goal state** (for
  example "the page shows 'Clicked'"), not as a step count. A deterministic
  end-state check decides whether it was reached; this is the seed of the
  verifier.
- **Every step is audited,** one JSONL line each (see below).

## The browser profile

MacPilot drives Playwright's own Chromium with a persistent profile used for
nothing else:

```
$MACPILOT_DATA_DIR/chromium-profile        (default: ~/.sensei/macpilot/chromium-profile)
```

It is disposable. To reset it, delete the directory, or from Python:

```
from macpilot.browser import reset_profile; reset_profile()
```

A profile path inside a real browser's data directory (Chrome, Chromium,
Brave or Edge) is refused.

## The audit log

`audit.jsonl` gets one line per step with these fields:
- `timestamp`, `step` and `action`.
- The normalized `coords`, the `pixels` they grounded to, and the `viewport`.
- The `stop_condition`, and the paths of the `screenshot` taken before the step
  and the `screenshot_after`.
- The `outcome`: `goal_reached`, `goal_not_reached` or `error`, with the
  `error_class` on errors.

The log never records page text, typed input or cookies. Screenshots are
referenced by path, and they stay on the machine.

## Setup, tests and demo

```
cd packages/macpilot
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m playwright install chromium     # Playwright's own Chromium (~100 MB)
.venv/bin/python -m pytest -q
.venv/bin/python -m macpilot.demo [--headed]          # the demo task
```

The end-to-end tests skip cleanly when Playwright or its Chromium is not
installed. The unit tests (coordinates, audit log, step budget, profile guard)
run either way.

**The demo task:** open the local fixture page (`macpilot/fixtures/button.html`)
and click the normalized point (500, 500), which lands on the button. Then
check the goal state, "the page shows 'Clicked'": the status text must change
from "Not clicked" to "Clicked". The demo prints the result and the paths of
the audit log and screenshots. Its step budget is 1.
