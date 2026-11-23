# Sensei 🥋

An open-source personal agent — Muse-class, built on open-weight models

**Status: Design Phase** — no agent code until the Design Review Gate passes.

## Design-phase steps

1. **Product Definition** — vision, users, personality, scope
2. **Architecture** — system design, model router, components
3. **Threat Model** — permissions, sandboxing, abuse and failure cases
4. **Eval Design** — SenseiBench, designed before code
5. **Design Review Gate** — sign-off required before implementation begins

See [DESIGN.md](DESIGN.md) for the design document and [STANDARDS.md](STANDARDS.md) for the engineering standards every component must meet.

## Repository layout

```
packages/core/       sensei_core — Python core package
packages/macpilot/   Sensei's hands — verified computer-use
packages/skills/     SkillForge format skills
apps/mac/            macOS app (placeholder)
apps/ios/            iOS app (placeholder)
evals/               SenseiBench (Step 4)
```

## License

Apache 2.0 — see [LICENSE](LICENSE). Model licences are listed in [NOTICE](NOTICE).
