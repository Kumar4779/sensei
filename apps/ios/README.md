# Sensei for iOS

A native iPhone client for Sensei, with a Watch companion for status and
approvals.

**Status: placeholder.** This directory contains no app code yet. On the
iPhone today, Sensei is used through the gateway's web UI, reached over the
user's own Tailscale tailnet (see [CHANGELOG.md](../../CHANGELOG.md), Slice 22).
A native client via TestFlight is the next step on the roadmap. An App Store
release comes after the product hardens.

## Place in the architecture

The iPhone and Watch are Surfaces in the [DESIGN.md](../../DESIGN.md) §2
system diagram: remote approval and chat surfaces for the Gateway on the Mac.
Nothing sensitive leaves the Mac unless the user allows it (Principle 5). The
Watch's v1 scope is status and approvals only.

## Tests and demo

None yet.
