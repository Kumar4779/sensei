# Slice 21 — Custodian: Sensei's memory served over MCP

## Why
The Custodian idea: one shared
memory that every tool reads and writes, so no tool has to be re-taught.
Sensei's MemoryStore (Slice 6) already has the hard properties — provenance
on every entry, gated writes, provable deletion. What it lacks is reach:
today only Sensei itself can use it. This slice serves the SAME store over
MCP so Claude Code (and any MCP client) shares it — with the gates intact.

## Change (packages/core, plus README note)
1. An MCP server (stdio transport, local only) exposing the existing
   MemoryStore — no changes to MemoryStore semantics:
   - `memory_search(query)` — concept search over ACTIVE entries
     (see point 2), returning entry ids + summaries, never raw files.
   - `memory_get(id)` — full entry incl. provenance.
   - `memory_propose(text, category)` — writes a NEW entry as PENDING with
     source=tool_content and the calling client named in origin. It can
     NEVER create an active entry. Activation stays Sensei-side
     (`confirm()`), exactly as Slice 6 designed.
   - `memory_list_pending()` — pending entries awaiting confirmation.
2. Embedding index as a DERIVED layer: a local index (embeddings computed
   locally via Ollama; model recorded in config) built FROM the entry
   files. The files remain the source of truth; deleting the index and
   rebuilding must reproduce it. Search = embedding similarity with a
   keyword fallback when the index is absent.
3. Dedup on propose: before writing, check similarity against active
   entries; if a near-duplicate exists, still write the pending entry but
   flag it `possible_duplicate_of: <id>` in its provenance — never
   silently drop, never silently merge.
4. Traces: every MCP read/write is traced like any other tool call
   (client, operation, entry id, counts — not full text of entries read).

## Frozen — do not touch
- MemoryStore file format, provenance fields, confirm/delete semantics.
- Permission manifest: the MCP server itself runs inside the gateway's
  trust boundary on localhost; no network listener beyond localhost.

## Acceptance
- Full suite green (163 tests at Slice 19 + Slice 20's additions — must
  not drop).
- New tests: propose→pending (never active), search finds a concept match
  whose keywords differ from the query, dedup flag on a near-duplicate
  propose, index rebuild reproduces search results, deletion of an entry
  removes it from search results.
- Live check: connect Claude Code (or the MCP inspector) to the server,
  propose an entry, show it pending; confirm it Sensei-side; search for
  it by concept from the client. Report what actually happened.
- Commit with the usual message style; report hash, test count, and the
  live-check result.
