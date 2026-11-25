# Slice 22 — Surfaces v1: web UI over the gateway (phone-first)

## Why
Requirement (2026-11-24): Sensei must be a product — graphical, usable on
iPhone and Apple Watch, eventually App Store-published.
Honest ladder: (1) web UI over the existing gateway — works on iPhone the
day it ships, no Apple review; (2) thin native iOS client via TestFlight
(December); (3) App Store after the product hardens. Watch v1 scope is
unchanged from the original spec: status + approvals companion only.
This slice is step 1.

## Change (packages/core gateway + a small static UI; no core-logic changes)
1. Serve a single-page web UI from the gateway (same FastAPI app, same
   token auth — the UI prompts for the token once and keeps it in
   sessionStorage only):
   - Morning brief: today's brief, rendered readably, with its
     verified/unverified status shown honestly.
   - Approvals: pending memory entries (from Slice 21's Custodian) with
     Confirm / Reject buttons calling the existing endpoints.
   - Costs: today's spend from the cost ledger, per route.
   - Kill switch: a visible Kill / Revive control calling /kill /revive.
2. Phone reachability, documented in the README (no code magic): the
   gateway stays localhost-bound by default; the supported way to reach
   it from the phone is Tailscale (or same-LAN with an explicit opt-in
   config flag that defaults OFF and logs a warning when enabled).
   Security boundary must not silently widen — Threat model rules apply.
3. Responsive layout: designed phone-width first, desktop second. No
   framework build step — plain HTML/CSS/JS served as static files, so
   the repo stays dependency-light.

## Frozen — do not touch
- Gateway endpoints' semantics and auth. UI is a client, nothing more.
- No new listener, no public exposure, no cloud hosting in this slice.

## Acceptance
- Full suite green (no test-count drop vs the previous slice).
- New tests: UI route requires auth (401 without token), static assets
  served, approvals page data matches /memory pending endpoint output.
- Live check: open the UI on the Mac, run a brief, confirm a pending
  memory entry from the UI, flip the kill switch and show /run refuse.
  Then open it from the iPhone browser (via the documented path) and
  repeat the approvals check. Report what actually happened on each.
- Commit with the usual message style; report hash, test count, and the
  live-check results (Mac + iPhone separately).

## Explicitly NOT this slice (December queue)
- Native iOS app + TestFlight, Watch companion app, App Store submission
  (needs Apple Developer Program + review), push notifications, cloud
  hosting. Each gets its own spec when its turn comes.
