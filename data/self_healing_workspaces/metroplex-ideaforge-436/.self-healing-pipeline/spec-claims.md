# Spec Claims — Elder-Care Safety & Support Companion (idea #436)

Source: `spec.md`. Domain: elder-care safety, mental health, protective
services — SAFETY-DOMAIN trigger fires.

## Stage 1A — Original spec claims

| id   | category    | claim                                                                                                  | spec source                       |
|------|-------------|--------------------------------------------------------------------------------------------------------|-----------------------------------|
| C-01 | BEHAVIOR    | Agent assists adult children monitoring an aging parent with mental health / frailty / cognitive issues | spec.md line 5 (Overview)         |
| C-02 | BEHAVIOR    | Agent helps user document incidents of verbal or physical aggression                                   | spec.md line 5 (Overview)         |
| C-03 | BEHAVIOR    | Agent provides safety resources                                                                        | spec.md line 5; line 39           |
| C-04 | BEHAVIOR    | Agent connects users to protective services / local support when needed                                | spec.md line 5; line 12           |
| C-05 | BEHAVIOR    | Agent assesses risk levels for the user (e.g. "fears for safety after argument")                       | spec.md line 5                    |
| C-06 | BEHAVIOR    | Agent ships an `agent.yaml` with name "Elder-Care Safety & Support Companion", model `claude-sonnet-4-6`, telegram_bot_token_env `ELDERCARE_BOT_TOKEN` | spec.md lines 10-15 |
| C-07 | BEHAVIOR    | Agent ships `skills/incident_logging/SKILL.md` (frontmatter: name "Incident Logging", description, trigger pattern) | spec.md lines 16-23 |
| C-08 | BEHAVIOR    | Agent ships `tests/test_e2e_sarah_logs_incident.py` (logs incidents + handles negations)               | spec.md line 24                   |
| C-09 | BEHAVIOR    | Agent ships `tests/test_e2e_sarah_requests_help.py` (provides safety resources)                        | spec.md line 25                   |
| C-10 | BEHAVIOR    | Agent ships a `README.md` with a four-paragraph story (Sarah intro, agent in action, invocation example, deployment notes) | spec.md line 26 |
| C-11 | BEHAVIOR    | Agent is invokable via `claude --agent eldercare` and `mission-cli`                                    | spec.md line 41 (Success criteria 3) |
| C-12 | BEHAVIOR    | E2E tests pass, including negation and word-boundary test cases                                        | spec.md line 40 (Success criteria 2) |
| C-13 | SAFETY      | Incident-logging matcher must REJECT negations such as "I didn't have an argument"                     | spec.md line 36 (Safety constraints) |
| C-14 | SAFETY      | Incident-logging matcher must REJECT word-boundary collisions such as "argumentative"                  | spec.md line 36 (Safety constraints) |
| C-15 | SAFETY      | Incident-logging matcher MUST LOG all silent drops (when a candidate phrase looked like it might match but did not, the system must record that decision so the parent has an audit trail) | spec.md line 36 (Safety constraints) |
| C-16 | SAFETY      | E2E test suite MUST include both positive AND negative test cases for the negation and word-boundary scenarios | spec.md line 36 (Safety constraints) |
| C-17 | CONSTRAINT  | Agent runs entirely on the user's CCOS instance (no remote runtime dependency)                         | spec.md line 29                   |
| C-18 | CONSTRAINT  | No external services or API keys are hardcoded                                                         | spec.md line 30                   |
| C-19 | CONSTRAINT  | Skills are bundled in the agent directory, not loaded from a global registry                           | spec.md line 31                   |
| C-20 | CONSTRAINT  | Single-purpose: focused on elder-care safety only                                                      | spec.md line 33                   |
| C-21 | OUT-OF-SCOPE| No web frontend in T1                                                                                  | spec.md line 32; line 47          |
| C-22 | OUT-OF-SCOPE| No multi-user partitioning                                                                             | spec.md line 44                   |
| C-23 | OUT-OF-SCOPE| No voice synthesis                                                                                     | spec.md line 45                   |
| C-24 | OUT-OF-SCOPE| No cross-session memory                                                                                | spec.md line 46                   |
| C-25 | OUT-OF-SCOPE| No calendar integration                                                                                | spec.md line 48                   |
| C-26 | OUT-OF-SCOPE| No external HTTP services                                                                              | spec.md line 49                   |
| C-27 | OUT-OF-SCOPE| No insurance claim submission                                                                          | spec.md line 50                   |
| C-28 | USER-VOICE  | Adult children writing about parents will use first-person free-text descriptions like "mom screamed at me again tonight", "she threw the remote", "I'm scared to come home" | spec.md line 5 narrative ("Sarah", "After a heated argument", "fears for her safety", "feels guilty"); user-voice trigger |
| C-29 | USER-VOICE  | The trigger pattern `incident\|argument\|aggression\|threat` describes the canonical tokens; real users will type many lay synonyms ("fight", "yelled", "shoved", "hit", "raised her voice", "lost it", "screamed", "got physical") | spec.md line 22 (trigger pattern); user-voice trigger |

### SAFETY-DOMAIN auto-claims (triggered by: elder care, mental health, life-safety, protective services)

| id   | category    | claim                                                                                                  | spec source                       |
|------|-------------|--------------------------------------------------------------------------------------------------------|-----------------------------------|
| C-30 | SAFETY      | Unrecognized / malformed / ambiguous input must NOT silently route to the lowest-severity outcome ("no incident detected"). Fail-safe direction is toward escalation, acknowledgement, or asking the user — not toward the soothing "nothing happened" branch. | safety-domain trigger (elder-care mental-health context) |
| C-31 | SAFETY      | Default branches must not assert "no incident matches" when the underlying truth is "the system did not understand the input"                                                          | safety-domain trigger             |
| C-32 | SAFETY      | When the user expresses fear for their own safety, the agent MUST surface safety resources (e.g. domestic-violence hotline, Adult Protective Services) regardless of whether a documentable incident was matched | spec.md line 5 ("Sarah fears for her safety"); safety-domain trigger |
| C-33 | SAFETY      | When the user expresses ambivalence/guilt ("I feel guilty seeking help"), the agent MUST validate the user without suppressing safety resources | spec.md line 5 ("feels guilty about seeking help"); safety-domain trigger |
| C-34 | SAFETY      | The agent MUST NOT make diagnostic claims about the parent ("your mother has X disease") — only describe behaviors | safety-domain trigger             |
| C-35 | SAFETY      | The agent MUST NOT direct the user to confront, restrain, or physically engage the parent — recommendations stay in {document, leave the room, call X resource, contact APS} | safety-domain trigger             |
| C-36 | SAFETY      | Incident logs MUST be persisted before the agent prints a confirmation — no claim of "logged" without write-then-read verification | safety-domain trigger (audit-trail integrity) |

## Stage 1A.5 — Adversarial-input mutations

Each derived row cites both the source claim AND the mutation axis.

### Mutations of C-13 (negation rejection)

| id   | category | claim                                                                                                  | derivation                       |
|------|----------|--------------------------------------------------------------------------------------------------------|----------------------------------|
| C-40 | SAFETY   | Negation variant: "I did not have an argument" must NOT be logged as an incident                       | derived from C-13; negation axis (full form) |
| C-41 | SAFETY   | Negation variant: "no argument happened" must NOT be logged                                            | derived from C-13; negation axis ("no") |
| C-42 | SAFETY   | Negation variant: "she wasn't aggressive" must NOT be logged                                           | derived from C-13; negation axis ("wasn't") |
| C-43 | SAFETY   | Negation variant: "there was no incident tonight" must NOT be logged                                   | derived from C-13; negation axis (modal subject) |
| C-44 | SAFETY   | Negation variant: "she never threatened me" must NOT be logged                                         | derived from C-13; negation axis ("never") |

### Mutations of C-14 (word-boundary collisions)

| id   | category | claim                                                                                                  | derivation                       |
|------|----------|--------------------------------------------------------------------------------------------------------|----------------------------------|
| C-45 | SAFETY   | Word-boundary: "argumentative" must NOT match the "argument" trigger                                   | derived from C-14; word-boundary axis (suffix) |
| C-46 | SAFETY   | Word-boundary: "incidental" must NOT match the "incident" trigger                                      | derived from C-14; word-boundary axis (suffix) |
| C-47 | SAFETY   | Word-boundary: "threatened" must match (this IS the asserted behavior in past tense), but "threading" must NOT | derived from C-14; word-boundary axis (distinguishing real morphology from collision) |
| C-48 | SAFETY   | Word-boundary: "passive-aggression" must NOT auto-trigger as physical "aggression" without further context (substring match alone is insufficient) | derived from C-14; word-boundary axis (compound) |

### Mutations of C-28 / C-29 (USER-VOICE — lay-register and embedded phrasing)

| id   | category | claim                                                                                                  | derivation                       |
|------|----------|--------------------------------------------------------------------------------------------------------|----------------------------------|
| C-50 | SAFETY   | Lay-register: "she yelled at me and threw the remote" must be recognized as an incident even though neither "argument" nor "aggression" appears as a literal token | derived from C-29; register-shift axis |
| C-51 | SAFETY   | Lay-register: "mom lost it and shoved me" must be recognized as an incident                            | derived from C-29; register-shift axis |
| C-52 | SAFETY   | Lay-register: "she got physical" must be recognized as an incident                                     | derived from C-29; register-shift axis |
| C-53 | SAFETY   | Embedded multi-token: "after dinner we had a huge fight and she threatened to kick me out" must match BOTH the fight/argument tier AND the threat tier (not just the first hit) | derived from C-29; embedded-symptom axis |
| C-54 | SAFETY   | Case-insensitive: "ARGUMENT" and "Argument" and "argument" must all match equivalently                 | derived from C-28; Unicode/case axis |

### Mutation of C-28 (Unicode normalization)

| id   | category | claim                                                                                                  | derivation                       |
|------|----------|--------------------------------------------------------------------------------------------------------|----------------------------------|
| C-55 | SAFETY   | Smart apostrophe variants: "she didn't apologize" (U+2019) must be treated identically to "she didn't apologize" (ASCII) for negation-detection purposes — both forms negate the asserted behavior | derived from C-13 + C-28; Unicode axis |

## Coverage budget

- USER-VOICE claims (C-28, C-29): 2
- Derived mutations from USER-VOICE: 6 (C-50..C-55)
- Mutation ratio: 3.0× — within the 1.5×–4× calibration window.
- SAFETY claims total: 23 (C-13..C-16, C-30..C-36, C-40..C-48, C-50..C-55)
- OUT-OF-SCOPE: 7
- BEHAVIOR / CONSTRAINT: 17

All claims are testable. Stage 1A and 1A.5 complete.
