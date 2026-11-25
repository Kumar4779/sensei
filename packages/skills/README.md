# Skills

Where Sensei's skills will live, in the SkillForge skill format.

**Status: placeholder.** This directory contains no skills and no code yet.
SkillForge itself is a separate project. Under [DESIGN.md](../../DESIGN.md)
Decision 2 it is to be consumed as a git submodule pinned to a specific
commit, and that submodule has not been added yet.

## Place in the architecture

Skills extend the [DESIGN.md](../../DESIGN.md) §2 Tools layer. They are
subject to the same Control Plane permission tiers as every built-in tool,
and to the malicious-skill defences of DESIGN.md §3 Threat 2, including
SkillForge's blind evals.

## Tests and demo

None yet.
