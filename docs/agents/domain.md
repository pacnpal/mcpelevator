# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

If either path doesn't exist, proceed silently. Don't flag its absence or suggest creating it upfront. The `/domain-modeling` skill, reached through `/grill-with-docs` and `/improve-codebase-architecture`, creates domain docs only when terms or decisions get resolved.

## File structure

This repo uses a single-context layout:

```text
/
|-- CONTEXT.md
|-- docs/
|   `-- adr/
|-- backend/
`-- frontend/
```

## Use the glossary's vocabulary

When your output names a domain concept in an issue title, refactor proposal, hypothesis, or test name, use the term defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept isn't in the glossary, either the proposed language doesn't belong in the project or the glossary has a real gap. Reconsider it or note it for `/domain-modeling`.

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it instead of silently overriding it:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because..._
