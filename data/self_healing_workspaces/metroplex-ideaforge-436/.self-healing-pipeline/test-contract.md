# Test Contract — Elder-Care Safety & Support Companion (idea #436)

Every test cites a spec-claims.md id. Pass criteria: all tests green AND
every spec-claims.md row has ≥1 test citing it.

## tests/test_e2e_sarah_logs_incident.py

| test | claim | input | expected |
|------|-------|-------|----------|
| test_logs_aggression_incident | C-01, C-02 | "Mom screamed at me and threw the remote tonight" | response.incident_logged is True; data/incidents.jsonl contains the record |
| test_handle_message_returns_calm_acknowledgment | C-01 | aggression text | response.text contains no diagnostic terms, opens with empathetic acknowledgment |
| test_negation_not_logged | C-13 | "I didn't have an argument with mom" | response.incident_logged is False; silent_drops.jsonl contains the input with reason "negated" |

## tests/test_e2e_sarah_requests_help.py

| test | claim | input | expected |
|------|-------|-------|----------|
| test_provides_safety_resources | C-03 | "I'm scared to come home" | response.resources is non-empty; contains at least one hotline number |
| test_risk_assessment_when_user_scared | C-05 | "I'm afraid she'll hurt me" | response.risk_level in {'elevated', 'high'} |
| test_guilt_phrase_validated_without_suppressing_resources | C-33 | "I feel guilty about seeking help" | response.text contains empathetic acknowledgment AND response.resources is non-empty |

## tests/test_matcher_negation.py

| test | claim | input | expected |
|------|-------|-------|----------|
| test_basic_negation_didnt | C-13 | "I didn't have an argument" | matched is False; silent_drop_reason == "negated" |
| test_negation_did_not | C-40 | "I did not have an argument" | matched is False |
| test_negation_no_subject | C-41 | "no argument happened" | matched is False |
| test_negation_wasnt | C-42 | "she wasn't aggressive" | matched is False |
| test_negation_modal_subject | C-43 | "there was no incident tonight" | matched is False |
| test_negation_never | C-44 | "she never threatened me" | matched is False |
| test_positive_control_argument | C-13 (positive) | "we had an argument" | matched is True; "argument" in matched_tokens |
| test_positive_control_aggressive | C-13 (positive) | "she was aggressive" | matched is True |

## tests/test_matcher_word_boundary.py

| test | claim | input | expected |
|------|-------|-------|----------|
| test_argumentative_does_not_match | C-14, C-45 | "she was being argumentative" | matched is False OR silent_drop_reason == "no_match"; "argument" NOT in matched_tokens |
| test_incidental_does_not_match | C-46 | "this was incidental" | matched is False; "incident" NOT in matched_tokens |
| test_threatened_does_match | C-47 (positive) | "she threatened me" | matched is True; threat tier present |
| test_threading_does_not_match | C-47 (negative collision) | "I was threading the needle" | matched is False |
| test_passive_aggression_needs_context | C-48 | "her passive-aggression is exhausting" | matched is False OR silent_drop_reason == "compound_needs_context" |
| test_positive_control_aggression_standalone | C-14 (positive) | "her aggression scared me" | matched is True |

## tests/test_matcher_lay_register.py

| test | claim | input | expected |
|------|-------|-------|----------|
| test_yelled_and_threw_remote | C-50 | "she yelled at me and threw the remote" | matched is True; multiple tiers present |
| test_lost_it_and_shoved | C-51 | "mom lost it and shoved me" | matched is True |
| test_got_physical | C-52 | "she got physical" | matched is True |
| test_embedded_multi_tier | C-53 | "after dinner we had a huge fight and she threatened to kick me out" | matched is True; tiers contains BOTH a fight/argument tier AND a threat tier |

## tests/test_matcher_unicode_and_case.py

| test | claim | input | expected |
|------|-------|-------|----------|
| test_uppercase_match | C-28, C-54 | "ARGUMENT" | matched is True |
| test_titlecase_match | C-54 | "Argument" | matched is True |
| test_lowercase_match | C-54 | "argument" | matched is True |
| test_smart_apostrophe_negation | C-55 | "she didn't apologize about the argument" (U+2019) AND "she didn't apologize about the argument" (ASCII) | both classify identically (both matched=True since they negate "apologize" not "argument"; argument is still positive) |
| test_smart_apostrophe_negation_blocks | C-55 (negation case) | "I didn't have an argument" with smart apostrophe | matched is False (negation still detected) |

## tests/test_silent_drop_audit.py

| test | claim | input | expected |
|------|-------|-------|----------|
| test_silent_drops_are_persisted | C-15 | "I didn't have an argument" | after handle_message: data/silent_drops.jsonl has a row with the raw input and a reason |
| test_unknown_input_does_not_say_no_incident | C-30, C-31 | "the weather was nice" (unknown / non-incident input) | response.text does NOT contain "no incident detected" or equivalent assertive negative; instead asks a clarifying question OR returns a neutral acknowledgement |
| test_logged_incident_round_trips_through_disk | C-36 | aggression input | log_incident returns Path; reading the file back yields the same record; if write fails, response.incident_logged is False (no false confirmation) |

## tests/test_agent_metadata.py

| test | claim | check |
|------|-------|-------|
| test_agent_yaml_fields | C-06 | agent.yaml has name "Elder-Care Safety & Support Companion", model "claude-sonnet-4-6", telegram_bot_token_env "ELDERCARE_BOT_TOKEN" |
| test_skill_md_frontmatter | C-07 | skills/incident_logging/SKILL.md frontmatter: name "Incident Logging", description present, trigger "incident\|argument\|aggression\|threat" |
| test_skills_bundled_in_agent_dir | C-19 | skills/ directory exists under agent root; SKILL.md is inside agent dir, not a sibling registry |

## tests/test_invocation_surface.py

| test | claim | check |
|------|-------|-------|
| test_agent_yaml_invocation | C-11 | agent.yaml exists and has the fields claude --agent eldercare would need (name field) |
| test_main_runnable | C-11 | agent.py has a main() and `if __name__ == "__main__"`; subprocess run with --message "test" returns 0 |

## tests/test_out_of_scope.py

| test | claim | check |
|------|-------|-------|
| test_no_remote_runtime_deps | C-17 | requirements.txt does not contain boto, aws-sdk, google-cloud, azure |
| test_no_hardcoded_api_keys | C-18 | source-file scan finds no sk-, AIza, ghp_, or other secret-shape strings (excluding the test file itself) |
| test_single_purpose | C-20 | README.md references elder-care only; no childcare, finance, healthcare scheduling |
| test_no_web_frontend | C-21 | no Flask, FastAPI, Django, Express, React, Vue in source or requirements |
| test_no_multi_user_partitioning | C-22 | no user_id partitioning in incident schema |
| test_no_voice_synthesis | C-23 | no pyttsx3, gTTS, elevenlabs in source/requirements |
| test_no_cross_session_memory | C-24 | no persistent conversation history outside incidents.jsonl/silent_drops.jsonl |
| test_no_calendar_integration | C-25 | no google-api-python-client, ical in source/requirements |
| test_no_external_http | C-26 | no requests, httpx, urllib3 import outside the disallow list (only stdlib urllib used for nothing — actually NONE) |
| test_no_insurance_claim | C-27 | source/README scan: no "insurance", "claim", "policy number" |

## tests/test_safety_resources.py

| test | claim | check |
|------|-------|-------|
| test_resources_surface_on_fear | C-03, C-32 | input "I'm scared" → resources list non-empty |
| test_resources_include_aps | C-04 | safety/resources.py SAFETY_RESOURCES includes an Adult Protective Services entry |
| test_resources_surfaced_without_match | C-32 | input expressing fear but NO incident verb ("I'm just scared, nothing happened yet") still surfaces resources |

## tests/test_no_diagnostic_or_confront_advice.py

| test | claim | input → expected |
|------|-------|------------------|
| test_no_diagnostic_claims | C-34 | aggression input → response.text contains none of {"diagnose", "diagnosis", "disorder", "borderline personality disorder", "schizophrenia", "bipolar"} |
| test_no_confront_advice | C-35 | aggression input → response.text contains none of {"confront", "restrain", "fight back", "hit back", "physically engage"} |

## tests/test_readme_structure.py

| test | claim | check |
|------|-------|-------|
| test_readme_has_four_paragraphs | C-10 | README.md split on blank lines yields exactly 4 paragraphs (excluding heading line); each paragraph addresses one of {intro, action, invocation, deployment} |

## tests/test_meta_coverage.py

| test | claim | check |
|------|-------|-------|
| test_e2e_sarah_logs_incident_file_exists | C-08 | file exists and contains an incident + negation test |
| test_e2e_sarah_requests_help_file_exists | C-09 | file exists and contains "resource" content |
| test_negation_and_word_boundary_test_files_exist | C-12, C-16 | both files exist and contain BOTH positive and negative test cases |
| test_canonical_trigger_tokens_covered | C-29 | test-contract.md exercises all four canonical trigger tokens (incident, argument, aggression, threat) |

## Pass criteria

1. All tests above pass under `pytest tests/ -v`.
2. Every spec-claims.md row id appears in the "claim" column of at least
   one row in this contract.
3. No test file is modified after the red commit — the failing tests at
   `red_commit_sha` are the immutable acceptance criteria.

## Setup / teardown

- All tests use `tmp_path` fixture for `data/` to avoid cross-test pollution.
- `conftest.py` at workspace root inserts the workspace into `sys.path` so
  tests can `from agent import handle_message` and `from matcher import
  IncidentMatcher`.
- No network access required.
