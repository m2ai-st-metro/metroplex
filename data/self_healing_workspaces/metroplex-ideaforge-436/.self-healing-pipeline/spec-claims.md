# Spec Claims — Elder-Care Safety & Support Companion (idea #436)

Source: `spec.md` in this workspace.

Safety-domain trigger: **FIRED** — spec mentions protective resources, abuse, self-neglect, safety protocols, risky behaviors, mother throwing objects, fear for safety. Adult-child caregiver under stress at 2 AM equivalent (sleepless nights, vigilance). Treat as life-impact safety domain.

## Stage 1A — Original spec claims

| id   | category    | claim                                                                                                       | spec source                                                  |
|------|-------------|-------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------|
| C-01 | BEHAVIOR    | Agent is named "Elder-Care Safety & Support Companion"                                                       | spec.md line 11                                              |
| C-02 | BEHAVIOR    | Agent ships an `agent.yaml` with `name`, `description`, `model`, `telegram_bot_token_env` fields            | spec.md lines 9-15                                           |
| C-03 | BEHAVIOR    | `agent.yaml` uses `telegram_bot_token_env: ELDER_CARE_BOT_TOKEN` as a placeholder env var name              | spec.md line 14; success criterion 5                         |
| C-04 | BEHAVIOR    | At least two skills are implemented under `skills/<skill_name>/` with a `SKILL.md` + Python implementation  | spec.md lines 16-22; success criterion 2                     |
| C-05 | BEHAVIOR    | Each skill `SKILL.md` has YAML frontmatter with `name`, `description`, `trigger` fields                     | spec.md lines 17-21                                          |
| C-06 | BEHAVIOR    | E2E tests live at `tests/test_e2e_*.py` covering the three named scenes                                     | spec.md lines 23-26                                          |
| C-07 | BEHAVIOR    | Test files include `test_e2e_mother_throws_objects_scene.py`, `test_e2e_sarah_seeks_help_scene.py`, and `test_e2e_incident_documentation_scene.py` | spec.md lines 24-26 |
| C-08 | BEHAVIOR    | README.md has a four-paragraph Scene opening: (1) Sarah/mother/Mother's Day argument intro, (2) agent helping Sarah document, (3) invocation example, (4) Telegram token configuration note | spec.md lines 27-31; success criterion 4 |
| C-09 | BEHAVIOR    | Agent is invokable via `claude --agent Elder-Care_Safety_Support_Companion`                                 | spec.md success criterion 1, line 45                         |
| C-10 | BEHAVIOR    | Incident Documentation skill records and categorizes incidents of risky behavior                            | spec.md lines 18-22 (frontmatter example)                    |
| C-11 | BEHAVIOR    | Incident Documentation trigger phrase is "document incident"                                                | spec.md line 21                                              |
| C-12 | BEHAVIOR    | A Safety Protocol skill exists (Safety constraints section names it as a second required skill)             | spec.md line 42                                              |
| C-13 | FAILURE     | Incident Documentation must NOT match negation cases like "I didn't document the incident"                  | spec.md line 41                                              |
| C-14 | FAILURE     | Incident Documentation must NOT match negation cases like "I almost documented it"                          | spec.md line 41                                              |
| C-15 | FAILURE     | Incident Documentation must NOT match word-boundary collisions like "documentary"                           | spec.md line 41                                              |
| C-16 | FAILURE     | Incident Documentation must NOT match word-boundary collisions like "documented" (as a fragment in another word) | spec.md line 41                                          |
| C-17 | SAFETY      | Incident Documentation must log at WARNING level if an incident is dropped (skipped/no-match)               | spec.md line 41                                              |
| C-18 | BEHAVIOR    | Incident Documentation includes positive/negative test pairs for true and false matches                     | spec.md line 41; success criterion 3                         |
| C-19 | FAILURE     | Safety Protocol must NOT match negation cases like "I didn't need help"                                     | spec.md line 42                                              |
| C-20 | FAILURE     | Safety Protocol must NOT match negation cases like "I almost called for help"                               | spec.md line 42                                              |
| C-21 | FAILURE     | Safety Protocol must NOT match word-boundary collisions like "helpless"                                     | spec.md line 42                                              |
| C-22 | FAILURE     | Safety Protocol must NOT match word-boundary collisions like "helper"                                       | spec.md line 42                                              |
| C-23 | SAFETY      | Safety Protocol must log at WARNING level if a safety request is dropped                                    | spec.md line 42                                              |
| C-24 | BEHAVIOR    | Safety Protocol includes positive/negative test pairs for true and false matches                            | spec.md line 42; success criterion 3                         |
| C-25 | OUT-OF-SCOPE| Multi-user partitioning is explicitly out of scope                                                          | spec.md line 52                                              |
| C-26 | OUT-OF-SCOPE| Voice synthesis is out of scope                                                                             | spec.md line 53                                              |
| C-27 | OUT-OF-SCOPE| Cross-session memory is out of scope                                                                        | spec.md line 54                                              |
| C-28 | OUT-OF-SCOPE| Web frontend is out of scope                                                                                | spec.md line 55                                              |
| C-29 | OUT-OF-SCOPE| Calendar integration is out of scope                                                                        | spec.md line 56                                              |
| C-30 | OUT-OF-SCOPE| External HTTP services are out of scope                                                                     | spec.md line 57                                              |
| C-31 | OUT-OF-SCOPE| Insurance claim submission is out of scope                                                                  | spec.md line 58                                              |
| C-32 | BEHAVIOR    | Agent runs entirely on the user's CCOS instance — bundled skills only, no global registry                   | spec.md lines 34, 36                                         |
| C-33 | BEHAVIOR    | No external services or API keys are hardcoded in source                                                    | spec.md line 35                                              |
| C-34 | USER-VOICE  | The user (Sarah) is an adult-child caregiver typing under stress (sleepless nights, vigilance) — input is free-text natural language describing incidents, not canonical command tokens | spec.md line 5; user-voice trigger from "Sarah at 2 AM"-equivalent scenario |
| C-35 | SAFETY      | Unrecognized / malformed / ambiguous input must NOT silently route to the lowest-severity outcome — fail-safe direction is toward the human (escalate, ask, refuse), not "everything is fine" | safety-domain trigger |
| C-36 | SAFETY      | Default branches must not assert "nothing matches" when the underlying truth is "the system didn't understand the input" — must log WARNING and surface ambiguity | safety-domain trigger; spec.md line 41-42 dropped-incident logging |
| C-37 | SAFETY      | If an incident describes physical danger (throwing objects, hitting), the system must NOT downgrade to "documentation only" — must surface safety-protocol path | spec.md line 5 (Mother's Day argument, objects thrown, fears for safety) |

## Stage 1A.5 — Adversarial-input mutations

Derived from [USER-VOICE] claim C-34 and from match-trigger claims C-11 ("document incident") and the implicit Safety Protocol trigger phrase.

| id   | category | claim                                                                                                                                            | derived from / axis                                |
|------|----------|--------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------|
| C-38 | SAFETY   | Smart apostrophe (U+2019) in user input must not bypass pattern matching — "I didn't document" with smart-quote must still be treated as negation| derived from C-13, C-14; Unicode axis              |
| C-39 | SAFETY   | Smart double-quote (U+201C/U+201D) around incident phrases must not bypass matching                                                              | derived from C-34; Unicode axis                    |
| C-40 | SAFETY   | "I didn't document the incident" must NOT trigger the Incident Documentation skill (negation form 1)                                             | derived from C-13; negation axis                   |
| C-41 | SAFETY   | "I almost documented it" must NOT trigger the Incident Documentation skill (negation/hypothetical form)                                          | derived from C-14; negation axis                   |
| C-42 | SAFETY   | "I never document anything" / "I won't document" must NOT trigger Incident Documentation                                                         | derived from C-13; negation axis                   |
| C-43 | SAFETY   | "I didn't need help" must NOT trigger Safety Protocol                                                                                            | derived from C-19; negation axis                   |
| C-44 | SAFETY   | "I almost called for help" must NOT trigger Safety Protocol                                                                                      | derived from C-20; negation axis                   |
| C-45 | SAFETY   | "I no longer need help" / "I'm not asking for help" must NOT trigger Safety Protocol                                                             | derived from C-19, C-20; negation axis             |
| C-46 | SAFETY   | "documentary" as a standalone word must NOT trigger Incident Documentation (word-boundary collision)                                             | derived from C-15; word-boundary axis              |
| C-47 | SAFETY   | "documented" appearing only as a noun/modifier ("a well-documented disease") must NOT trigger; full trigger phrase needs the verb-form context   | derived from C-16; word-boundary axis              |
| C-48 | SAFETY   | "helpless" as a standalone word must NOT trigger Safety Protocol                                                                                 | derived from C-21; word-boundary axis              |
| C-49 | SAFETY   | "helper" / "helpers" as standalone words must NOT trigger Safety Protocol                                                                        | derived from C-22; word-boundary axis              |
| C-50 | SAFETY   | Lay-register paraphrases ("write this down", "log this", "record what just happened", "I need to make a note") must match Incident Documentation at the same severity as the canonical "document incident" | derived from C-34; register-shift axis |
| C-51 | SAFETY   | Lay-register paraphrases of help-seeking ("I'm scared", "I need someone", "this isn't safe", "call somebody") must route to Safety Protocol, not silently fall through | derived from C-34; register-shift axis |
| C-52 | SAFETY   | Canonical trigger embedded in a longer sentence ("can you help me document the incident that happened tonight, she threw a vase at me") must still match Incident Documentation | derived from C-11, C-34; embedded-symptom axis |
| C-53 | SAFETY   | Canonical trigger embedded with multiple intents ("I need help and want to document what happened") must trigger BOTH Safety Protocol and Incident Documentation, not just the first match | derived from C-11, C-34; embedded-symptom axis |
| C-54 | SAFETY   | Case-insensitive matching: "DOCUMENT INCIDENT" / "Document Incident" / "document INCIDENT" must all trigger Incident Documentation                | derived from C-11; case-axis (substring matching implicit) |

## Coverage summary

- BEHAVIOR rows: 14 (C-01 to C-12, C-18, C-24, C-32, C-33)
- FAILURE rows: 8 (C-13 to C-16, C-19 to C-22)
- SAFETY rows: 20 (C-17, C-23, C-35 to C-54 minus duplicates)
- USER-VOICE rows: 1 (C-34)
- OUT-OF-SCOPE rows: 7 (C-25 to C-31)
- TONE / UX rows: 0 (spec does not make explicit tone claims; the agent description implies supportive tone but no testable promise)

USER-VOICE → SAFETY/FAILURE mutation ratio: 1 USER-VOICE claim → 17 derived mutations. Well above the 1.5× floor; calibrated against the safety-domain weight of this build.
