# Domain Docs

This is a single-context repository. Engineering skills should consume its domain
documentation as follows.

## Before exploring

- Read `CONTEXT.md` at the repository root when it exists.
- Read ADRs under `docs/adr/` that touch the area being changed.
- If either location does not exist, proceed silently.

Producer skills create domain documentation lazily when terminology or architectural
decisions are resolved; consumers should not require empty placeholder files.

## Vocabulary

Use the terms defined in `CONTEXT.md` in tests, issues, plans, and implementation. If a
needed concept is absent, reconsider whether it is project language or note the gap for a
future domain-documentation session.

## ADR conflicts

Surface conflicts with an existing ADR explicitly instead of silently overriding it.
