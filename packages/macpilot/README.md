# MacPilot

Sensei's hands: verified computer use on the Mac, starting with the browser.

**Status: placeholder.** This directory contains no code yet. MacPilot cannot
perform any task today, including perception (screenshots and element
grounding). The composition demo (`scripts/composition_demo.py`) inspects this
directory and reports the MacPilot leg as not done for exactly this reason.

## Place in the architecture

MacPilot is a tool in the [DESIGN.md](../../DESIGN.md) §2 Tools layer
(browser and desktop), reached by the Agent Runtime through the same
permission tiers as every other tool. The design fixes these points in
advance:

- It operates in a dedicated browser profile, never the user's real profile.
- Every action is checked by the verifier against the resulting state, not
  against the model's description of what it did.
- SenseiBench will report a perception ablation (screenshot only, against
  screenshot plus accessibility/DOM data), named in DESIGN.md §4.

## Tests and demo

None yet. The first planned milestone is one verified browser task with a
SenseiBench task and checker of its own.
