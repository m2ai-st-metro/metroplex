# Implementation Plan — Elder-Care Safety & Support Companion (idea #436)

## Summary

Build a single-purpose CCOS agent that helps adult children document
incidents of aggression from an aging parent and connects them to safety
resources. Core engine is an `IncidentMatcher` that does negation-aware,
word-boundary-aware, lay-synonym-aware classification of free-text user
input, then writes durable incident logs and emits safety-resource
guidance when warranted.

## Target directory layout

```
metroplex-ideaforge-436/
├── agent.yaml
├── README.md                                      # 4-paragraph Sarah story
├── requirements.txt
├── conftest.py                                    # pytest sys.path shim
├── agent.py                                       # public entrypoint: handle_message(text, state)
├── matcher.py                                     # IncidentMatcher + classification
├── skills/
│   └── incident_logging/
│       ├── SKILL.md                               # frontmatter per spec
│       ├── __init__.py
│       └── implementation.py                      # log_incident, list_incidents
├── safety/
│   ├── __init__.py
│   └── resources.py                               # SAFETY_RESOURCES table + lookup
├── data/                                          # gitignored runtime dir
│   └── (incidents.jsonl is created at runtime)
└── tests/
    ├── __init__.py
    ├── test_e2e_sarah_logs_incident.py
    ├── test_e2e_sarah_requests_help.py
    ├── test_matcher_negation.py
    ├── test_matcher_word_boundary.py
    ├── test_matcher_lay_register.py
    ├── test_matcher_unicode_and_case.py
    ├── test_silent_drop_audit.py
    ├── test_agent_metadata.py                     # agent.yaml + skill structure
    ├── test_invocation_surface.py                 # `claude --agent eldercare` + mission-cli
    ├── test_out_of_scope.py                       # no web/voice/HTTP/calendar/insurance
    ├── test_safety_resources.py                   # surfaced regardless of incident match
    ├── test_no_diagnostic_or_confront_advice.py
    └── test_readme_structure.py
```

## Implementation steps

1. **agent.yaml** — exact fields per spec (C-06).

2. **matcher.py** — `IncidentMatcher` class:
   - Normalize input: NFC Unicode normalization, lowercase, ASCII-fold smart
     apostrophes (U+2019 → `'`), collapse whitespace.
   - Tokenize on word boundaries (regex `\b\w+(?:'\w+)?\b`).
   - **Negation detection**: scan token windows of size ≤ 3 BEFORE each
     candidate trigger token for any of {"not", "no", "didn't", "did not",
     "wasn't", "was not", "isn't", "is not", "never", "wouldn't", "would not",
     "haven't", "has not", "hasn't"}. If found in window, drop with reason
     "negated".
   - **Word-boundary matching**: match canonical triggers
     `{argument, aggression, threat, incident, fight, yelled, screamed, shoved,
     hit, threw, lost it, got physical, raised her voice, kicked, slapped,
     pushed, choked}` against the tokenized form. Substring matches against
     LONGER words (e.g. "argumentative" contains "argument" as substring but
     the token is "argumentative", not "argument") MUST NOT trigger.
   - **Lay-register synonyms**: maintain a mapping of lay phrases to canonical
     incident tiers; matched via the same tokenization and negation pipeline.
   - **Compound caveats**: tokens that appear inside compound words like
     "passive-aggression" produce a `requires_context` flag rather than an
     auto-match — log a silent-drop with reason "compound_needs_context".
   - **Return shape**: `MatchResult(matched: bool, matched_tokens: list[str],
     tiers: set[str], silent_drop_reason: Optional[str], raw_input: str,
     normalized: str)`.
   - Every classification decision is loggable for audit (C-15).

3. **skills/incident_logging/implementation.py**:
   - `log_incident(record: dict, data_dir: Path) -> Path` — append JSON line
     to `data/incidents.jsonl`, fsync, re-read last line to confirm
     (write-then-read verification per C-36).
   - `list_incidents(data_dir: Path) -> list[dict]`.
   - `log_silent_drop(reason: str, raw_input: str, data_dir: Path) -> Path`
     — appends to `data/silent_drops.jsonl` (C-15).

4. **skills/incident_logging/SKILL.md** — exact frontmatter per spec (C-07).

5. **safety/resources.py**:
   - SAFETY_RESOURCES table: National Domestic Violence Hotline
     (1-800-799-7233), National Elder Care Locator (1-800-677-1116),
     APS-by-state generic referral instructions, 988 Suicide & Crisis Lifeline.
   - `surface_safety_resources(user_text: str) -> list[Resource]` that fires
     when the user expresses fear / safety concern, regardless of incident
     match (C-32). Phrases: "scared", "afraid", "fear", "unsafe", "in danger",
     "need help", "what should I do".

6. **agent.py** — `handle_message(text: str, state: AgentState) -> Response`:
   - Run `IncidentMatcher.classify(text)`.
   - If matched: persist incident (C-36), include confirmation.
   - If matched silent-drop: log audit row, ask user a disambiguating question
     instead of returning "no incident" (C-30, C-31).
   - Always run `surface_safety_resources(text)` and merge results.
   - Apply guard rails: response text MUST NOT contain words from a
     diagnostic-claim blocklist ("diagnose", "diagnosis", "disorder",
     "borderline personality disorder", "schizophrenia") or a
     confront-advice blocklist ("confront", "restrain", "fight back",
     "hit back"). Strip / refuse if generated (C-34, C-35).
   - Validate user emotion ("I feel guilty") with a fixed empathetic preface
     when the input matches an ambivalence phrase, without suppressing the
     resource list (C-33).

7. **README.md** — exactly four paragraphs:
   1. Introduce Sarah, her mother, and the situation.
   2. Show the agent in action: example transcript.
   3. Invocation example: `claude --agent eldercare` and
      `mission-cli run eldercare --message "..."`.
   4. Deployment configuration: env vars, where state lives, T1 scope notes.

8. **Tests** — see test-contract.md. All E2E tests use the public
   `handle_message` API. Unit tests target `matcher.py` and
   `safety/resources.py` directly.

9. **Invocation surface** — `agent.py` exposes a `if __name__ == "__main__":`
   block that reads stdin and runs `handle_message`, so both
   `claude --agent eldercare` (Claude's agent CLI scans `agent.yaml`) and a
   shim called from `mission-cli` (executes `python agent.py`) work. The
   test for C-11 verifies the CLI surface exists by importing `agent.main`
   and checking the script is executable.

## Risk areas and edge cases

- **Smart-apostrophe variants**: must NFC-normalize before any token compare.
- **Compound words**: "passive-aggression", "argumentative", "incidental" —
  the substring/word-boundary distinction is the #1 silent-fallback failure
  mode for this domain.
- **Embedded multi-tier inputs**: one sentence with both "fight" AND "threat"
  must register both (C-53). The tokenizer must scan the whole input, not
  short-circuit on first match.
- **Mocked persistence in tests**: tests use `tmp_path` for `data_dir`.
- **Guild rails on response text**: the guard runs as a post-processing pass
  on the final response string, not as a separate path — easier to test.

## Spec-claim coverage

Every spec-claims.md id → at least one test:

- C-01, C-02 → test_e2e_sarah_logs_incident.py::test_logs_aggression_incident
- C-03, C-32 → test_safety_resources.py::test_resources_surface_on_fear
- C-04 → test_safety_resources.py::test_resources_include_aps
- C-05 → test_e2e_sarah_requests_help.py::test_risk_assessment_when_user_scared
- C-06 → test_agent_metadata.py::test_agent_yaml_fields
- C-07 → test_agent_metadata.py::test_skill_md_frontmatter
- C-08 → tests/test_e2e_sarah_logs_incident.py (file existence + content)
- C-09 → tests/test_e2e_sarah_requests_help.py (file existence + content)
- C-10 → test_readme_structure.py::test_readme_has_four_paragraphs
- C-11 → test_invocation_surface.py::{test_agent_yaml_invocation, test_main_runnable}
- C-12 → meta: all E2E tests must pass (verified by running pytest)
- C-13, C-40..C-44 → test_matcher_negation.py (one test per claim)
- C-14, C-45..C-48 → test_matcher_word_boundary.py (one test per claim)
- C-15 → test_silent_drop_audit.py::test_silent_drops_are_persisted
- C-16 → meta: test_matcher_negation.py + test_matcher_word_boundary.py contain BOTH positive and negative cases
- C-17 → test_out_of_scope.py::test_no_remote_runtime_deps (requirements.txt has no boto/aws/gcp/azure SDK; agent.yaml has no remote runtime field)
- C-18 → test_out_of_scope.py::test_no_hardcoded_api_keys (source scan)
- C-19 → test_agent_metadata.py::test_skills_bundled_in_agent_dir
- C-20 → test_out_of_scope.py::test_single_purpose (README mentions only elder-care, no other domains)
- C-21 → test_out_of_scope.py::test_no_web_frontend
- C-22 → test_out_of_scope.py::test_no_multi_user_partitioning
- C-23 → test_out_of_scope.py::test_no_voice_synthesis
- C-24 → test_out_of_scope.py::test_no_cross_session_memory
- C-25 → test_out_of_scope.py::test_no_calendar_integration
- C-26 → test_out_of_scope.py::test_no_external_http
- C-27 → test_out_of_scope.py::test_no_insurance_claim
- C-28, C-54 → test_matcher_unicode_and_case.py (case-insensitive)
- C-29, C-50..C-53 → test_matcher_lay_register.py (one test per claim)
- C-30, C-31 → test_silent_drop_audit.py::test_unknown_input_does_not_say_no_incident
- C-33 → test_e2e_sarah_requests_help.py::test_guilt_phrase_validated_without_suppressing_resources
- C-34 → test_no_diagnostic_or_confront_advice.py::test_no_diagnostic_claims
- C-35 → test_no_diagnostic_or_confront_advice.py::test_no_confront_advice
- C-36 → test_silent_drop_audit.py::test_logged_incident_round_trips_through_disk
- C-55 → test_matcher_unicode_and_case.py::test_smart_apostrophe_negation

## Red confirmation

Ran `pytest tests/ -v` against the workspace before any implementation
code was written. Result: **9 collection errors** (one per test file
that imports `agent` or `matcher`), zero passing tests. Exit code 2.

```
ERROR tests/test_e2e_sarah_logs_incident.py — ModuleNotFoundError: No module named 'agent'
ERROR tests/test_e2e_sarah_requests_help.py — ModuleNotFoundError: No module named 'agent'
ERROR tests/test_matcher_lay_register.py — ModuleNotFoundError: No module named 'matcher'
ERROR tests/test_matcher_negation.py — ModuleNotFoundError: No module named 'matcher'
ERROR tests/test_matcher_unicode_and_case.py — ModuleNotFoundError: No module named 'matcher'
ERROR tests/test_matcher_word_boundary.py — ModuleNotFoundError: No module named 'matcher'
ERROR tests/test_no_diagnostic_or_confront_advice.py — ModuleNotFoundError: No module named 'agent'
ERROR tests/test_safety_resources.py — ModuleNotFoundError: No module named 'agent'
ERROR tests/test_silent_drop_audit.py — ModuleNotFoundError: No module named 'agent'
!!! Interrupted: 9 errors during collection !!!
```

The metadata / out-of-scope / readme / invocation tests collect but
will fail their assertions because the corresponding files don't exist
yet (agent.yaml, SKILL.md, README.md, agent.py).

Red is confirmed: no test passes accidentally; nothing to weaken.
