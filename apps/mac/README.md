# Sensei for macOS

A native macOS menu-bar app for Sensei.

**Status: placeholder.** This directory contains no app code yet. On the Mac
today, Sensei is used through the gateway's web UI
(`http://127.0.0.1:8765/ui`; see [`packages/core`](../../packages/core/README.md)).

## Place in the architecture

The Mac app is one of the Surfaces in the [DESIGN.md](../../DESIGN.md) §2
system diagram. Like every surface, it is a client of the Gateway: the agent
runtime, memory and traces stay in `sensei_core` on the Mac (DESIGN.md
Principle 5, local-first).

## Tests and demo

None yet.
