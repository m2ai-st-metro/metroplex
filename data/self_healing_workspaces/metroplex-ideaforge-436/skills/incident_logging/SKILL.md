---
name: Incident Logging
description: Logs incidents of verbal or physical aggression from the elderly parent, with negation-aware and word-boundary-aware matching.
trigger: "incident|argument|aggression|threat"
---

# Incident Logging

The Incident Logging skill records discrete events of verbal or physical
aggression from the aging parent so the adult-child caregiver has an
auditable history they can share with their own therapist, a social
worker, or Adult Protective Services.

## Matching rules

- **Case-insensitive substring matching** is the *baseline* requested by the
  spec (SKILL.md line 59 in the original spec). It is overlaid with:
  - **Word-boundary anchoring** — `argumentative` does not trigger the
    `argument` rule; `incidental` does not trigger `incident`.
  - **Negation detection** — `I didn't have an argument`, `no argument
    happened`, `she wasn't aggressive`, `there was no incident tonight`,
    `she never threatened me` are all silently dropped (with an audit
    row, never a fake confirmation).
  - **Unicode normalization** — smart apostrophes (U+2019) are folded
    to ASCII before negation detection.
  - **Lay-register synonyms** — `yelled`, `screamed`, `lost it`,
    `shoved`, `got physical`, `raised her voice` map onto the same
    incident tiers as the canonical clinical-sounding tokens.
  - **Compound caveat** — `passive-aggression` does NOT auto-trigger the
    physical `aggression` rule; it is recorded as
    `compound_needs_context` for follow-up.

## Persistence

Every classification decision (match, negated, no_match,
compound_needs_context) is durable. Confirmed incidents land in
`data/incidents.jsonl`; silent drops land in `data/silent_drops.jsonl`.
The agent never reports "logged" without write-then-read verification.

## Out of scope (T1)

No web frontend, no voice synthesis, no multi-user partitioning, no
cross-session memory, no calendar integration, no external HTTP calls,
no insurance claim submission. Runs entirely on the user's CCOS instance.
