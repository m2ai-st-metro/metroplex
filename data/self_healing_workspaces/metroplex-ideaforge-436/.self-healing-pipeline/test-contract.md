# Test Contract — Elder-Care Safety & Support Companion (idea #436)

Pass criteria: **all tests below pass** AND **every row in `spec-claims.md` has ≥1 test citing it**.

## tests/test_agent_invocation.py

| test                                                | covers   | input                                                  | expected                                                       |
|-----------------------------------------------------|----------|--------------------------------------------------------|----------------------------------------------------------------|
| test_agent_yaml_has_correct_name                    | C-01     | Load `agent.yaml`                                      | `name == "Elder-Care Safety & Support Companion"`              |
| test_agent_yaml_has_required_fields                 | C-02     | Load `agent.yaml`                                      | Has keys: `name`, `description`, `model`, `telegram_bot_token_env` |
| test_agent_yaml_telegram_env_placeholder            | C-03     | Load `agent.yaml`                                      | `telegram_bot_token_env == "ELDER_CARE_BOT_TOKEN"`             |
| test_agent_invocation_string_matches_spec           | C-09     | Read README.md text                                    | Contains `claude --agent Elder-Care_Safety_Support_Companion`  |

## tests/test_skill_structure.py

| test                                                | covers   | input                                                  | expected                                                       |
|-----------------------------------------------------|----------|--------------------------------------------------------|----------------------------------------------------------------|
| test_at_least_two_skills_exist                      | C-04     | List dirs under `skills/`                              | ≥2 skill dirs, each with `SKILL.md` + `implementation.py`      |
| test_skill_md_has_frontmatter                       | C-05     | Read each `skills/*/SKILL.md`                          | Each has YAML frontmatter with `name`, `description`, `trigger`|
| test_e2e_test_files_exist                           | C-06     | Glob `tests/test_e2e_*.py`                             | ≥3 files                                                       |
| test_three_specific_e2e_files_exist                 | C-07     | Direct path check                                      | All three named files exist                                    |
| test_incident_documentation_trigger_phrase          | C-11     | Read `skills/incident_documentation/SKILL.md` frontmatter | `trigger == "document incident"`                             |
| test_safety_protocol_skill_exists                   | C-12     | Path check                                             | `skills/safety_protocol/SKILL.md` and `implementation.py` exist |
| test_skills_are_bundled_locally                     | C-32     | Inspect skills dir                                     | No code imports from a global skill registry path              |

## tests/test_readme_structure.py

| test                                                | covers   | input                                                  | expected                                                       |
|-----------------------------------------------------|----------|--------------------------------------------------------|----------------------------------------------------------------|
| test_readme_has_four_scene_paragraphs               | C-08     | Read `README.md`                                       | Scene section contains exactly 4 paragraphs in correct order: Sarah/mother/Mother's Day intro, documentation help, invocation example, Telegram token note |

## tests/test_e2e_mother_throws_objects_scene.py

| test                                                | covers   | input                                                  | expected                                                       |
|-----------------------------------------------------|----------|--------------------------------------------------------|----------------------------------------------------------------|
| test_physical_danger_triggers_safety_protocol       | C-37     | dispatch("my mom just threw a vase at me, i need help")| Result list contains a Safety Protocol response                |
| test_multi_intent_triggers_both_skills              | C-53     | dispatch("I need help and want to document the incident — she threw a glass") | Result list contains BOTH Safety Protocol AND Incident Documentation |

## tests/test_e2e_sarah_seeks_help_scene.py

| test                                                | covers   | input                                                  | expected                                                       |
|-----------------------------------------------------|----------|--------------------------------------------------------|----------------------------------------------------------------|
| test_explicit_help_request_triggers_safety_protocol | C-12     | dispatch("I really need help right now")               | Result contains Safety Protocol                                |
| test_lay_register_im_scared_triggers_safety         | C-51     | dispatch("I'm scared and don't know what to do")       | Result contains Safety Protocol                                |
| test_lay_register_call_somebody_triggers_safety     | C-51     | dispatch("can you call somebody for me")               | Result contains Safety Protocol                                |

## tests/test_e2e_incident_documentation_scene.py

| test                                                | covers   | input                                                  | expected                                                       |
|-----------------------------------------------------|----------|--------------------------------------------------------|----------------------------------------------------------------|
| test_incident_is_recorded_and_categorized           | C-10     | dispatch("please document this incident")              | Result contains Incident Documentation with `logged=True` and a `category` field |
| test_canonical_trigger_in_long_sentence             | C-52     | dispatch("can you help me document the incident that happened tonight, she threw a vase at me") | Result contains Incident Documentation |
| test_lay_register_record_what_happened_triggers     | C-50     | dispatch("can you record what just happened")          | Result contains Incident Documentation                         |

## tests/test_safety_constraints.py

| test                                                | covers   | input                                                  | expected                                                       |
|-----------------------------------------------------|----------|--------------------------------------------------------|----------------------------------------------------------------|
| test_negation_didnt_document                        | C-13, C-40 | dispatch("I didn't document the incident")           | NOT triggered — Incident Documentation NOT in result           |
| test_negation_almost_documented                     | C-14, C-41 | dispatch("I almost documented it")                   | NOT triggered                                                  |
| test_negation_never_document                        | C-42     | dispatch("I never document anything")                  | NOT triggered                                                  |
| test_word_boundary_documentary                      | C-15, C-46 | dispatch("I watched a documentary last night")       | NOT triggered (no Incident Documentation)                      |
| test_word_boundary_documented_fragment              | C-16, C-47 | dispatch("This is a well-documented disease")        | NOT triggered                                                  |
| test_warning_log_on_dropped_incident                | C-17, C-36 | dispatch("the sky is blue today") with logging capture| WARNING-level log emitted with message indicating dropped input |
| test_negation_didnt_need_help                       | C-19, C-43 | dispatch("I didn't need help")                       | NOT triggered — Safety Protocol NOT in result                  |
| test_negation_almost_called_for_help                | C-20, C-44 | dispatch("I almost called for help")                 | NOT triggered                                                  |
| test_negation_no_longer_need_help                   | C-45     | dispatch("I no longer need help")                      | NOT triggered                                                  |
| test_word_boundary_helpless                         | C-21, C-48 | dispatch("I feel helpless")                          | NOT triggered                                                  |
| test_word_boundary_helper                           | C-22, C-49 | dispatch("my helper is on her way")                  | NOT triggered                                                  |
| test_warning_log_on_dropped_safety_request          | C-23, C-36 | dispatch("the weather is nice") with logging capture | WARNING-level log emitted                                      |
| test_unicode_smart_apostrophe_in_negation           | C-38     | dispatch("I didn’t document the incident")        | NOT triggered                                                  |
| test_unicode_smart_double_quote                     | C-39     | dispatch("“I need help”, she said")          | Triggered (Safety Protocol) — quotes must not block matching   |
| test_lay_register_documentation_synonyms            | C-50     | dispatch each of {"please write this down","log this for me","I need to make a note about tonight"} | Each triggers Incident Documentation |
| test_lay_register_safety_synonyms                   | C-51     | dispatch each of {"I'm scared","this isn't safe","I need someone here"} | Each triggers Safety Protocol |
| test_case_insensitive_document_incident             | C-54     | dispatch each of {"DOCUMENT INCIDENT","Document Incident","document INCIDENT"} | Each triggers Incident Documentation |
| test_unrecognized_input_does_not_silent_succeed     | C-35     | dispatch("zxcvbnm random gibberish 12345")             | Returns empty list AND emits WARNING                           |

## tests/test_spec_out_of_scope.py

| test                                                | covers   | input                                                  | expected                                                       |
|-----------------------------------------------------|----------|--------------------------------------------------------|----------------------------------------------------------------|
| test_no_web_frontend                                | C-28     | Repo scan                                              | No `app.py` / FastAPI/Flask/Django dependency; no HTML in source |
| test_no_calendar_integration                        | C-29     | Repo scan                                              | No imports of `googleapiclient.discovery` / `caldav` / `icalendar` / `google.calendar` |
| test_no_external_http_services                      | C-30     | Repo scan                                              | No imports of `requests` / `httpx` / `urllib.request.urlopen` calls in source (excluding tests) |
| test_no_insurance_claim_submission                  | C-31     | Repo scan                                              | Source contains no functions named `*insurance*claim*` / `*submit*claim*` |
| test_no_multi_user_partitioning                     | C-25     | Repo scan                                              | No `user_id` / `tenant_id` partitioning columns in source       |
| test_no_voice_synthesis                             | C-26     | Repo scan                                              | No imports of `pyttsx3` / `gTTS` / `elevenlabs` / `openai.audio` |
| test_no_cross_session_memory                        | C-27     | Repo scan                                              | No persistent storage like sqlite/postgres connection setup     |
| test_no_hardcoded_secrets                           | C-33     | Repo scan                                              | No literal AWS/Anthropic/OpenAI/Telegram API key patterns       |

## Setup / teardown

- A `conftest.py` at workspace root injects the workspace path into `sys.path` so `import agent` and `import matcher` resolve.
- `tests/__init__.py` makes `tests` a package.
- Tests must NOT modify global state; logging-capture tests use `caplog` fixture.
- The `test_spec_out_of_scope.py` negative-existence scans MUST exclude the test file's own directory from the search scope to avoid self-matching.

## Spec-claim coverage (final)

Every C-01 through C-54 row in `spec-claims.md` is cited by at least one test in this contract. Out-of-scope rows (C-25..C-31) get explicit negative tests. USER-VOICE C-34 is covered transitively via its Stage 1A.5 derived mutations (C-38..C-54).
