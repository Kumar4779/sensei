# doc-summary-001: SenseiBench dev task (document summary)

**Category:** document summary · **Version:** 1 · **Split:** public dev
**Sensitivity:** API_OK · **Workflow:** `doc_summary` · **Tiers touched:** read

## What it tests

Can Sensei turn an article into a structured summary without dropping its key
facts or pulling in material the article marks as unrelated? The output is
`summary.json`, a JSON object with these fields:

- `title`: the article's headline.
- `key_points`: a list of the main facts, with names and figures as written.
- `entities`: the named organisations, people and places.
- `abstract`: one sentence.

This is the first SenseiBench task whose content is **API_OK by design**. The
source is a synthetic, public-style news article with no personal data, so it
may go to an API route. It runs on `cheap_api` (Kimi, Slice 35), unlike every
earlier task, which is LOCAL_ONLY. It is a capability task, not a safety probe.

## Package (DESIGN.md §4 Part 2, rule 1)

| Part | File |
|---|---|
| Instruction (user-style, no tool names or steps) | `task.json` → `instruction` |
| Workflow | `task.json` → `workflow` (`doc_summary`; the bench dispatches on it) |
| Fixture snapshot | `fixture/source.md` (579 words) |
| Checker | `checker.py` |
| Checker ground truth | `expected.json` (beside the checker, outside `fixture/`) |
| Gold output (for checker tests) | `gold-summary.json` |
| Budget | `task.json` → `budget` (max_cost_usd 0.10 estimated per trial, max_retries 1) |
| Permission tiers touched | `task.json` → `tiers_touched` |

## The fixture

`fixture/source.md` is an invented article about the Kestrel Bay Tidal Array,
an invented tidal-energy project off the invented town of Port Elsby. Its facts
are unambiguous: 18 megawatts, about 9,000 homes, 26 months of construction,
64 million pounds, and a second phase by 2029. The named entities are
Marrowfield Energy, the Elsby Harbour Authority, the engineer Dana Okafor and
Port Elsby.

It ends with a **distractor**, a section headed "Also in this issue
(unrelated to the tidal array)". That section is about a library book festival
at Greywater Hall with the novelist Tomas Verhoek, 2,400 visitors and a date of
19 June. A correct summary leaves all of it out.

## How it is checked (rule 3: final state, never transcripts)

```
python checker.py path/to/summary.json [--json]
```

All checks are deterministic. A summary passes only if:

- the file is a JSON object with `title` (string), `key_points` (list of
  strings), `entities` (list of strings) and `abstract` (string), none empty;
- `abstract` is exactly one sentence;
- each key fact appears **verbatim in the right field**:
  - `title` contains "Kestrel Bay Tidal Array";
  - `key_points` contains each of the 5 figures;
  - `entities` contains each of the 4 names;
  - `abstract` contains "Kestrel Bay";
- no distractor fact appears in any field. This check is case-insensitive,
  so changing capitalisation does not hide a leak.

The workflow also checks its own output before it is graded. Those checks come
from the input alone: the shape, entities grounded in the main article, one
sentence, and no distinctive term from the section the source marks as
unrelated. They are not the checker's facts. The workflow never sees
`expected.json`, `checker.py` or the gold summary; a trial's sandbox holds
only `fixture/source.md` (Slice 23).

## Known limits (rule 7, difficulty honesty)

- **It is the easy baseline for the category.** There is one short document,
  the facts are stated once and plainly, and the distractor is clearly marked.
  A harder version needs long or messy sources, facts that must be combined,
  and an unmarked off-topic passage.
- **Substring checks have two blind spots.** A figure reworded ("nine
  thousand homes", "18 MW") fails even though it is right. A summary that
  pastes the right strings in the right fields passes whatever its quality.
  Key-point quality, ordering, duplication and faithfulness beyond the listed
  facts are unchecked.
- **The distractor list is finite.** A leak that uses none of the listed terms
  (for example "a library event") is not caught.

## Versioning (rule 6)

Changing the instruction, fixture, checker or `expected.json` requires bumping
`version` in `task.json` and re-running affected numbers. No silent edits.
