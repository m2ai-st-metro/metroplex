# Judge Brief — Attempt 1

**Verdict: PASS**

## Evidence

### Independent test run

```
$ /home/apexaipc/projects/metroplex/venv/bin/python -m pytest \
    /home/apexaipc/projects/metroplex/data/self_healing_workspaces/metroplex-ideaforge-436/tests/
============================== 57 passed in 0.13s ==============================
```

Run independently from the Builder's reported output — same result.

### Immutability check

```
$ git diff 6759dfd -- data/self_healing_workspaces/metroplex-ideaforge-436/tests/
(empty)
```

No diff against the red-fix anchor `6759dfd45a02dd62d04f58a939670b09959c19b8`.
Builder did not modify, weaken, or delete any committed test.

NOTE: the red commit was extended once during attempt 1 with a
`[red-fix]` commit (`6759dfd`) to add `tests/test_meta_coverage.py`
and re-state-contract entries for C-08, C-09, C-12, C-16, C-29 (file-
existence / suite-shape claims the Planner originally satisfied
implicitly). This was a Planner-phase repair — the Builder's
implementation work was unaffected — and the red-fix anchor became
the new immutability baseline.

### Builder checklist verification

| Builder claim | Verified by Judge |
|---------------|-------------------|
| agent.yaml created with spec fields | yes — test_agent_yaml_fields passes |
| matcher.py with negation / word-boundary / Unicode / lay-register | yes — 26 matcher tests all pass |
| safety/resources.py with APS + DV hotline + 988 | yes — test_resources_include_aps passes |
| skills/incident_logging/ bundled in agent dir | yes — test_skills_bundled_in_agent_dir passes |
| README.md as 4-paragraph Sarah story | yes — test_readme_has_four_paragraphs passes |
| Out-of-scope guards green | yes — all 11 test_out_of_scope tests pass |
| Iteration log captures the negation-window narrowing and README rewording | yes — see builder-log-1.md "Iteration history within attempt 1" |

### Spec-claim coverage table

51 unique claim ids in `spec-claims.md`; **51/51** covered by at least
one test that cites the id.

```
$ python3 -c '<coverage script above>'
Unique claims: 51
Claims missing from tests: []
Claims missing from contract: []
Orphan ids in tests: []
```

Spot-check for genuine (non-tautological) coverage on the
adversarial / safety claims:

- C-13 (negation rejection): `test_basic_negation_didnt` passes
  realistic free-text "I didn't have an argument", not a canonical
  fixture mirrored from the implementation rule set. ✓
- C-29 / C-50–C-53 (lay-register): tests exercise "lost it and
  shoved me", "got physical", "yelled at me and threw the remote" —
  these don't appear verbatim in the matcher rule table; they're
  matched via the lay-synonym mechanism. ✓
- C-32 (resources without match): `test_resources_surfaced_without_match`
  uses "I'm just scared, nothing happened yet" — no incident token
  present; verifies the fail-safe surfacing path. ✓
- C-36 (write-then-read): `test_logged_incident_round_trips_through_disk`
  reads the JSONL back and verifies the record matches. ✓
- C-55 (smart apostrophe): tests BOTH the ASCII form and the U+2019
  form and verifies they classify identically. ✓
- C-30/C-31 (unknown input does not say "no incident detected"):
  `test_unknown_input_does_not_say_no_incident` passes "the weather
  was nice today" and asserts the response does NOT contain the
  forbidden assertive negatives. ✓

No fake or tautological coverage was detected.

### Out-of-scope existence-scan exclusion check

`tests/test_out_of_scope.py` excludes both `THIS_FILE` and the entire
`tests/` directory from its source scan, so the forbidden-pattern
strings in the test file itself don't make the test mechanically
unpassable. Verified by inspection of `_source_files()`.

## Why PASS (all four required)

1. Tests pass independently — 57/57 green.
2. No test-file diff against `red_commit_sha` (`6759dfd`).
3. Every spec-claims.md row has ≥1 test citing it; spot-check
   confirms coverage is genuine, not tautological.
4. Every test cites a real spec-claims.md id (no orphans).

## What to watch in the post-PASS dark-code-audit step

- The matcher's negation window (3 tokens) is a heuristic. Future
  spec retries that introduce longer-distance negation phrasings
  ("I will categorically deny that there was ever any argument
  whatsoever") may fail. Worth a follow-up adversarial-input pass.
- The lay-synonym table is a static set. A future Ravage review may
  surface lay phrasings I didn't anticipate (mirrors the #427 r2
  postscript pattern). The next retry, if any, should pre-emptively
  enumerate additional axes via Stage 1A.5.

These are notes for the dark-code audit, not blocking issues.
