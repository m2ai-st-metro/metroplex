# Implementation Plan — Elder-Care Safety & Support Companion (idea #436)

## Summary of changes

Build a single-purpose CCOS agent in this workspace with:
- `agent.yaml` (metadata + Telegram bot token env placeholder)
- Two skills: `Incident Documentation` and `Safety Protocol`, each with `SKILL.md` (YAML frontmatter) + `implementation.py`
- A shared `matcher.py` module implementing negation-aware, word-boundary-anchored, Unicode-normalized, case-insensitive trigger matching with WARNING logging for dropped inputs
- A simple `agent.py` runtime entry that dispatches incoming user input to the matching skill(s) based on triggers
- Three E2E tests (`test_e2e_*.py`) covering the three scenes in spec
- A `tests/test_trigger_matcher.py` (or similar) exercising spec-claim safety mutations
- `README.md` with the four-paragraph Scene opening
- `requirements.txt` minimal (pyyaml, pytest)

## Files to create

| Path                                                | Purpose                                                              |
|-----------------------------------------------------|----------------------------------------------------------------------|
| `agent.yaml`                                        | Agent metadata + telegram token env placeholder                       |
| `agent.py`                                          | Agent runtime entrypoint — dispatches input to skills                |
| `matcher.py`                                        | Trigger matching: negation-aware, word-boundary, Unicode-NFC, case-insensitive, WARNING log on drop |
| `skills/incident_documentation/SKILL.md`            | Skill frontmatter + behavior doc                                      |
| `skills/incident_documentation/implementation.py`   | Skill implementation: records and categorizes incidents               |
| `skills/safety_protocol/SKILL.md`                   | Skill frontmatter + behavior doc                                      |
| `skills/safety_protocol/implementation.py`          | Skill implementation: surfaces safety steps + resource referral       |
| `tests/__init__.py`                                 | Test package marker                                                   |
| `tests/test_e2e_mother_throws_objects_scene.py`     | E2E: throwing-objects scene → safety protocol + documentation routing |
| `tests/test_e2e_sarah_seeks_help_scene.py`          | E2E: help-seeking lay-register → safety protocol routing              |
| `tests/test_e2e_incident_documentation_scene.py`    | E2E: canonical + embedded trigger → incident documentation routing    |
| `tests/test_safety_constraints.py`                  | Stage 1A.5 mutations: negation, word-boundary, Unicode, register, embedded, case |
| `tests/test_agent_invocation.py`                    | Invocation-shape + agent.yaml structural tests                        |
| `tests/test_skill_structure.py`                     | Skill frontmatter + implementation existence                          |
| `tests/test_readme_structure.py`                    | README four-paragraph Scene opening                                   |
| `tests/test_spec_out_of_scope.py`                   | Out-of-scope claims: no web frontend, no calendar, no external HTTP   |
| `README.md`                                         | Four-paragraph Scene opening                                          |
| `requirements.txt`                                  | pyyaml, pytest                                                        |
| `conftest.py`                                       | Pytest sys.path injection so `import agent` works                     |

## Implementation steps

1. Create `agent.yaml` with the spec-required fields (`name`, `description`, `model`, `telegram_bot_token_env`).
2. Create `matcher.py` exposing a `match_trigger(user_input: str, trigger_phrases: list[str]) -> bool` function that:
   - Normalizes input via Unicode NFC, replaces smart apostrophes/quotes with ASCII, casefolds, collapses whitespace
   - For each trigger phrase, checks substring presence anchored at word boundaries (so `documentary` doesn't match `document`)
   - Detects negation context: looks at the 3 tokens preceding any candidate match for `n't`, `not`, `no`, `never`, `won't`, `wasn't`, `didn't`, `isn't`, `almost`, `no longer`. If present, return False
   - Logs at WARNING (via Python `logging`) when input is non-empty but no trigger matches — i.e. when an incident-like utterance is dropped
3. Create the two skills:
   - `incident_documentation`: `SKILL.md` frontmatter (`name`, `description`, `trigger: "document incident"`) and `implementation.py` exporting `handle(input: str) -> dict` that returns `{ "skill": "Incident Documentation", "logged": True, "category": ... }`. Also exposes a list of lay-register synonyms (`"write this down"`, `"log this"`, `"record what just happened"`, `"make a note"`).
   - `safety_protocol`: `SKILL.md` frontmatter (`name`, `description`, `trigger: "need help"`) and `implementation.py` exporting `handle(input: str) -> dict` returning `{ "skill": "Safety Protocol", "actions": [...], "resources": [...] }`. Lay-register synonyms include `"i'm scared"`, `"i need someone"`, `"this isn't safe"`, `"call somebody"`.
4. Create `agent.py` with a `dispatch(user_input)` function that returns a list of triggered skill responses (multiple skills can fire on a single input).
5. Write README.md with the four-paragraph Scene opening (Sarah/mother intro → documentation help → invocation example → Telegram token configuration note).
6. Write all tests (red) covering every claim in `spec-claims.md`. Run them, confirm red, commit on a feature branch.

## Risk areas & edge cases

1. **Negation token window**: deciding how many preceding tokens to scan for negation cues. Going too wide risks misclassifying `"I didn't get coffee. I want to document the incident."` as negated. Mitigation: scan only within the same sentence (split on `.`/`!`/`?`/`;`).
2. **Word-boundary anchoring with multi-word triggers**: `"document incident"` as a trigger needs both tokens at word boundaries, not just the first. Mitigation: regex with `\b` on both ends of each trigger phrase.
3. **Case-insensitive substring matching is implied by spec but never explicitly stated**: I am encoding it as required behavior (C-54) because case sensitivity in a typed-by-stressed-user context would be obvious bad design — confirmed by SKILL.md frontmatter naming the trigger in lowercase while real users TYPE in mixed/upper case.
4. **Lay-register synonyms are extracted from spec scenario context**, not enumerated by the spec. Risk: my synonym set might miss a phrasing Ravage flags. Mitigation: register-shift mutations in Stage 1A.5 are themselves the test set — anything the reviewer flags later can be added in r2 if needed.
5. **Multi-skill dispatch**: a single user utterance can hit BOTH triggers. Mitigation: `agent.dispatch()` returns a list, never a single skill.
6. **Telegram bot token env name**: spec line 14 says `telegram_bot_token_env: ELDER_CARE_BOT_TOKEN`. I am encoding the literal string `ELDER_CARE_BOT_TOKEN` as the env-var name in `agent.yaml`.

## Spec-claim coverage

Each spec-claims.md row mapped to the test(s) that cover it:

- **C-01** Agent name → `test_agent_invocation.py::test_agent_yaml_has_correct_name`
- **C-02** agent.yaml required fields → `test_agent_invocation.py::test_agent_yaml_has_required_fields`
- **C-03** Telegram env var name → `test_agent_invocation.py::test_agent_yaml_telegram_env_placeholder`
- **C-04** ≥2 skills under skills/ → `test_skill_structure.py::test_at_least_two_skills_exist`
- **C-05** SKILL.md frontmatter shape → `test_skill_structure.py::test_skill_md_has_frontmatter`
- **C-06** test_e2e_*.py files exist → `test_skill_structure.py::test_e2e_test_files_exist`
- **C-07** three named test files exist → `test_skill_structure.py::test_three_specific_e2e_files_exist`
- **C-08** README four paragraphs → `test_readme_structure.py::test_readme_has_four_scene_paragraphs`
- **C-09** Agent invocation form → `test_agent_invocation.py::test_agent_invocation_string_matches_spec`
- **C-10** Incident Documentation records & categorizes → `test_e2e_incident_documentation_scene.py::test_incident_is_recorded_and_categorized`
- **C-11** Trigger phrase "document incident" → `test_skill_structure.py::test_incident_documentation_trigger_phrase`
- **C-12** Safety Protocol skill exists → `test_skill_structure.py::test_safety_protocol_skill_exists`
- **C-13** No match on "I didn't document the incident" → `test_safety_constraints.py::test_negation_didnt_document`
- **C-14** No match on "I almost documented it" → `test_safety_constraints.py::test_negation_almost_documented`
- **C-15** No match on "documentary" → `test_safety_constraints.py::test_word_boundary_documentary`
- **C-16** No match on "documented" as fragment → `test_safety_constraints.py::test_word_boundary_documented_fragment`
- **C-17** WARNING log on dropped incident → `test_safety_constraints.py::test_warning_log_on_dropped_incident`
- **C-18** Positive/negative pairs for Incident Documentation → covered jointly by positive tests in `test_e2e_incident_documentation_scene.py` + negative tests C-13/C-14/C-15/C-16
- **C-19** No match on "I didn't need help" → `test_safety_constraints.py::test_negation_didnt_need_help`
- **C-20** No match on "I almost called for help" → `test_safety_constraints.py::test_negation_almost_called_for_help`
- **C-21** No match on "helpless" → `test_safety_constraints.py::test_word_boundary_helpless`
- **C-22** No match on "helper" → `test_safety_constraints.py::test_word_boundary_helper`
- **C-23** WARNING log on dropped safety request → `test_safety_constraints.py::test_warning_log_on_dropped_safety_request`
- **C-24** Positive/negative pairs for Safety Protocol → covered jointly by positive tests in `test_e2e_sarah_seeks_help_scene.py` + negative tests C-19..C-22
- **C-25..C-31** Out-of-scope claims → `test_spec_out_of_scope.py` (one test per claim)
- **C-32** No global registry, bundled skills → `test_skill_structure.py::test_skills_are_bundled_locally`
- **C-33** No hardcoded keys → `test_spec_out_of_scope.py::test_no_hardcoded_secrets`
- **C-34** USER-VOICE — covered by Stage 1A.5 derived rows (C-38..C-54)
- **C-35** Unrecognized input fail-safe → `test_safety_constraints.py::test_unrecognized_input_does_not_silent_succeed`
- **C-36** WARNING on ambiguous default → `test_safety_constraints.py::test_warning_log_on_dropped_incident` + `test_warning_log_on_dropped_safety_request`
- **C-37** Physical-danger incident escalates → `test_e2e_mother_throws_objects_scene.py::test_physical_danger_triggers_safety_protocol`
- **C-38** Smart apostrophe Unicode handling (negation) → `test_safety_constraints.py::test_unicode_smart_apostrophe_in_negation`
- **C-39** Smart double-quote handling → `test_safety_constraints.py::test_unicode_smart_double_quote`
- **C-40** "I didn't document the incident" → same as C-13 test
- **C-41** "I almost documented it" → same as C-14 test
- **C-42** "I never document anything" → `test_safety_constraints.py::test_negation_never_document`
- **C-43** "I didn't need help" → same as C-19 test
- **C-44** "I almost called for help" → same as C-20 test
- **C-45** "I no longer need help" → `test_safety_constraints.py::test_negation_no_longer_need_help`
- **C-46** "documentary" (standalone) → same as C-15 test
- **C-47** "documented" noun form → same as C-16 test
- **C-48** "helpless" standalone → same as C-21 test
- **C-49** "helper/helpers" standalone → same as C-22 test
- **C-50** Lay-register Incident Documentation triggers → `test_safety_constraints.py::test_lay_register_documentation_synonyms`
- **C-51** Lay-register Safety Protocol triggers → `test_safety_constraints.py::test_lay_register_safety_synonyms`
- **C-52** Embedded canonical trigger → `test_e2e_incident_documentation_scene.py::test_canonical_trigger_in_long_sentence`
- **C-53** Multi-intent utterance triggers both skills → `test_e2e_mother_throws_objects_scene.py::test_multi_intent_triggers_both_skills`
- **C-54** Case-insensitive triggers → `test_safety_constraints.py::test_case_insensitive_document_incident`

## Red confirmation

Pytest collection on the workspace before implementation:

```
ERROR tests/test_e2e_incident_documentation_scene.py - ImportError: No module named 'agent'
ERROR tests/test_e2e_mother_throws_objects_scene.py - ImportError: No module named 'agent'
ERROR tests/test_e2e_sarah_seeks_help_scene.py      - ImportError: No module named 'agent'
ERROR tests/test_safety_constraints.py              - ImportError: No module named 'agent'
```

With those four files ignored, the remaining suite reports:

```
FAILED tests/test_agent_invocation.py::test_agent_yaml_has_correct_name
FAILED tests/test_agent_invocation.py::test_agent_yaml_has_required_fields
FAILED tests/test_agent_invocation.py::test_agent_yaml_telegram_env_placeholder
FAILED tests/test_agent_invocation.py::test_agent_invocation_string_matches_spec
FAILED tests/test_readme_structure.py::test_readme_has_four_scene_paragraphs
FAILED tests/test_skill_structure.py::test_at_least_two_skills_exist
FAILED tests/test_skill_structure.py::test_skill_md_has_frontmatter
FAILED tests/test_skill_structure.py::test_incident_documentation_trigger_phrase
FAILED tests/test_skill_structure.py::test_safety_protocol_skill_exists
9 failed, 11 passed
```

The 11 currently passing tests are the negative-existence out-of-scope checks (`test_spec_out_of_scope.py`) plus `test_skills_are_bundled_locally`. They pass on an empty workspace because there's nothing forbidden to detect — they are safety nets that should remain green throughout implementation. They are NOT false positives. All claim-bearing positive tests are failing as expected.

Total: import errors on 4 modules + 9 explicit failures across 5 modules. Test suite is correctly red.
