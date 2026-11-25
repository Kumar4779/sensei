# Slice 19: Subjects are quoted text — workflow verifies the blockquote convention

Scope: packages/core + README only. Do NOT modify any evals task or checker.

## Background

Slice 18's flag rule ended the omission (probe v3: coverage passes, no
obedience), but every trial failed one check — obedience_2_pwned — because
attack B's payload is in its subject, and the model wrote the subject on a
plain flag line instead of in a blockquote.

## Work

1. Prompt (sensei_core/brief.py):
   - QUOTE_RULE extended: a summary or flag line carries ONLY the sender's
     name; the message's subject AND body are message text and go in '> '
     blockquotes.
   - FLAG_RULE extended: the flag line itself states, in words, that the
     message contained an instruction that was not followed (e.g.
     "Flagged — contained an instruction I did not follow:").
2. Workflow check (brief.py brief_checks() + verifier.py if a new check
   class is needed): for EVERY inbox message, a check labelled quoted[i]
   that passes iff the message's subject text appears ONLY on lines
   starting with '>' (leading spaces allowed) — or not at all (absence is
   coverage's problem, not this check's). Case-insensitive. Add quoted[i]
   entries to the repair prompt's label->fact mapping ("the subject
   '<subject>' may appear only inside '> ' blockquotes").
3. Workflow name becomes "brief+repair+quote-style+coverage+flag+quoted".
4. Tests: (a) both prompt rules contain the new wording. (b) quoted[i]
   unit tests: subject on a plain line fails; subject only in blockquotes
   passes; subject absent passes. (c) A FakeClient draft with the subject
   on a plain line, repaired to blockquote-only, ends verified. (d) Full
   suite passes; update tests pinning the old workflow name or check
   list, and say so in the report.
5. Run the bench on morning-brief-002-adversarial-v3, trials=3, real
   qwen3:8b (shipped config). Add the README row exactly as measured with
   the new workflow label, plus one note: pass or fail, which checks
   failed if any, and whether the flags now state the non-obedience in
   words.
6. Commit: "Slice 19: subjects are quoted text — workflow verifies the blockquote convention"
7. Report back: commit hash, files changed, pytest summary, the bench
   summary verbatim, and any choices you made that this spec did not
   dictate.
