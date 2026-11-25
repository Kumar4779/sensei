# MacPilot v1 benchmark: 20 tasks

The tasks MacPilot's browser agent is measured on. They come from the five
task patterns in the MacPilot spec's eval section, with four concrete tasks per
pattern.

**Status: task list only.** The fixture sites, the test-account reset scripts
and the checkers are not built yet. Each task becomes a SenseiBench task
package (`task.json`, fixture, `checker.py`), following the rules in
[`evals/README.md`](../README.md), when MacPilot can attempt it.

**Rules for every task**
- **Browser only.** Tasks run in MacPilot's disposable Chromium profile, never
  the user's real profile.
- **Test accounts only.** Fixture sites are served locally. The Gmail and
  Calendar tasks use dedicated test accounts that hold no real personal data.
- **Fixed starting state.** Every trial starts from a known snapshot: fixture
  sites are static, and test accounts are reset to a seeded state.
- **Checkable without a model.** Success is an end state, checked
  deterministically: a file's contents or SHA-256, a fixture server's
  submission log, or a Gmail or Calendar API query. The agent's own account of
  what it did never counts.
- **Outward actions need approval.** Submitting a form or creating anything
  others can see waits for the user's approval, and a denied approval must
  leave no trace.
- **Perception baseline:** screenshot only, with coordinates normalized to
  0–1000 (MacPilot Slice 1). AX/DOM perception is a later challenger arm.

Output files go in the trial's output directory, written `out/` below.

## Pattern 1: Search, compare two options, and return a cited summary table

| ID | Goal | Starting state | Success condition (end state) |
|---|---|---|---|
| MP-01 | Search the shop for the two laptops "Aero 14" and "Nimbus 13" and compare price, weight and battery life | Local fixture shop at its home page, empty cart, fixed catalogue | `out/compare.csv` has exactly 2 rows (one per laptop), with price, weight and battery values equal to the fixture catalogue's, and each row's `source` is that product's page URL |
| MP-02 | Compare the two direct flights SFO→NRT on 2026-12-18 by departure time, duration and fare | Local fixture travel site, search form empty, fixed schedule | `out/flights.csv` has exactly the 2 direct flights, values equal to the fixture schedule, each row citing its result-page URL |
| MP-03 | Compare two coffee grinders from their separate review pages: burr type, grind settings, price | Two fixture review pages, reached from the review site's search | `out/grinders.csv` has 2 rows with values equal to the review pages', each row citing the review URL it came from |
| MP-04 | Compare the "Team" and "Business" plans: monthly price, seat limit, storage | Fixture SaaS pricing page in monthly-billing view | `out/plans.csv` has 2 rows matching the fixture's monthly prices (not annual), seat limits and storage, each citing the pricing URL |

## Pattern 2: Fill and, with approval, submit a test form correctly

| ID | Goal | Starting state | Success condition (end state) |
|---|---|---|---|
| MP-05 | Fill the contact form with the given name, email and message, and submit it after approval | Fixture contact form, empty, submission log empty | The fixture's submission log holds exactly 1 submission with exactly the given field values, and the trace shows the approval granted before the submit |
| MP-06 | Complete the 3-step signup (account details, preferences with a select, a radio and a checkbox, then review) and submit after approval | Fixture signup flow at step 1, no test user registered | Exactly 1 test user is registered, with every field (including select, radio and checkbox values) equal to the given spec; approval precedes submission |
| MP-07 | Submit the expense form; the first attempt meets a validation error (the phone number format), which must be corrected | Fixture expense form, empty, validation enabled | Exactly 1 accepted submission with the corrected phone number and all other values exact; no rejected submission is left stored |
| MP-08 | Fill the contact form, request approval, and stop when the approval is **denied** | Fixture contact form, empty, submission log empty; approval set to deny | The submission log is empty (0 submissions), and the trace shows the denial and no submit request |

## Pattern 3: Download a file, rename it to a spec, verify it exists

| ID | Goal | Starting state | Success condition (end state) |
|---|---|---|---|
| MP-09 | Download the November 2026 invoice from the billing page and save it as `invoice-2026-11.pdf` | Fixture account (test login) on its billing page with 3 invoices listed | `out/invoice-2026-11.pdf` exists, and its SHA-256 equals the fixture's November invoice; no other files in `out/` |
| MP-10 | Export orders for October 2026 as CSV from the dashboard and save it as `orders-2026-10.csv` | Fixture dashboard, date filter at its default (the current month) | `out/orders-2026-10.csv` exists, and its SHA-256 equals the fixture's October export, so the wrong filter fails |
| MP-11 | From a page with three attachments, download only "Floor plan (rev B)" and save it as `floor-plan-rev-b.pdf` | Fixture project page listing revs A, B and C | `out/floor-plan-rev-b.pdf` exists with rev B's SHA-256, and revs A and C were not downloaded |
| MP-12 | Download the most recent release notes (by date, not by list position) and save them as `release-notes-latest.pdf` | Fixture releases page listing 5 versions, not in date order | `out/release-notes-latest.pdf` exists with the SHA-256 of the most recently dated release |

## Pattern 4: Find specific information across 3+ pages and extract it to a file

| ID | Goal | Starting state | Success condition (end state) |
|---|---|---|---|
| MP-13 | Find the company's office address (Contact page), founding year (About page) and CEO's name (Team page) | Fixture company site at its home page | `out/facts.json` equals `{"address": …, "founded": …, "ceo": …}` with the fixture's exact values |
| MP-14 | Collect the default timeout of the `http`, `db` and `queue` components from their three separate docs pages | Fixture docs site at its index page | `out/timeouts.json` maps each of the 3 components to the exact default stated on its page |
| MP-15 | Extract every product priced under $20.00 across the 4 pages of a paginated listing | Fixture shop listing at page 1 of 4, fixed catalogue | `out/under-20.csv` holds exactly the fixture's set of products under $20.00 (names and prices), none missing and none extra |
| MP-16 | Find the dates of three named events, each on a different page of an events site | Fixture events site at its home page | `out/events.json` maps the 3 event names to their exact ISO dates |

## Pattern 5: Complete a multi-step task in a test Gmail or Calendar account

| ID | Goal | Starting state | Success condition (end state) |
|---|---|---|---|
| MP-17 | Find the email from `vendor@test` with the subject "Quote #4471" and draft (not send) a reply accepting the quote | Test Gmail account reset to its seeded mailbox | The Gmail API shows exactly 1 new draft in that thread containing the acceptance phrase given in the task, and no new sent messages |
| MP-18 | Create the event "Design review" on 2026-12-08, 10:00–10:30 (account time zone), with no guests, after approval | Test Calendar reset to its seeded week, that slot free | The Calendar API shows exactly 1 event with that title, start, end and no attendees; approval precedes creation |
| MP-19 | Apply the label "Receipts" to every email from `billing@test` and archive them | Test Gmail account reset; 4 seeded emails from that sender, label absent | The Gmail API shows all 4 messages labelled "Receipts" and out of the inbox, with no other message changed |
| MP-20 | Move the seeded "1:1" event from Thursday 2026-12-10 to Friday 2026-12-11 at the same time | Test Calendar reset; the event exists on Thursday | The Calendar API shows the event (same ID) on Friday at the same start and end, with no duplicate left on Thursday |
