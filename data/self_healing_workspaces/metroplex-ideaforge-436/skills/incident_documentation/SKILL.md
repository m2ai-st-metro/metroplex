```yaml
name: Incident Documentation
description: Records and categorizes incidents of risky behavior so Sarah has a calm, structured record she can take to her care team.
trigger: "document incident"
```

# Incident Documentation

When Sarah needs to write down what just happened — a thrown object, a refusal
to take medication, an episode of self-neglect — this skill captures the
incident with a category (verbal, physical, self-neglect, financial, other)
and a calm acknowledgement that the record was kept.

## Triggers

The canonical trigger phrase is `document incident`. The skill also responds
to lay-register paraphrases real adult-child caregivers use under stress:

- `write this down`
- `log this`
- `record what just happened`
- `make a note` (within the context of an incident, not a casual reminder)

## Safety constraints

This skill must NEVER fire on:

- Negation cases: `I didn't document the incident`, `I almost documented it`,
  `I never document anything`.
- Word-boundary collisions: `documentary`, `well-documented disease`,
  `documented citizens`. The matcher requires the trigger tokens to appear
  at word boundaries.
- Smart-apostrophe negation forms (`I didn’t document`) — the matcher
  normalizes Unicode punctuation before scanning.

If the user input is non-empty but no trigger matched, the dispatcher logs
at WARNING level with the dropped utterance for later review. This is the
spec's "log at WARNING level if an incident is dropped" requirement.
