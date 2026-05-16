```yaml
name: Safety Protocol
description: Surfaces immediate safety steps and connects Sarah to protective resources when she is in danger or describes self-neglect / physical risk.
trigger: "need help"
```

# Safety Protocol

When Sarah is in danger — her mother threw something, she's locked in a
bathroom, she's afraid to sleep — this skill surfaces a calm, ordered set
of safety steps and connects her to local protective resources (Adult
Protective Services, the National Domestic Violence Hotline, her local
non-emergency police line). It is the agent's "you are not alone right
now" response.

## Triggers

The canonical trigger phrase is `need help`. Lay-register paraphrases
real adult-child caregivers use under stress also fire:

- `i'm scared`
- `i need someone`
- `this isn't safe`
- `call somebody`
- `please help`

A physical-danger context (thrown object, hitting, locked in a room)
should ALSO trigger Safety Protocol even if the user did not explicitly
ask for help — the matcher delegates that to the dispatcher's
physical-danger heuristic.

## Safety constraints

This skill must NEVER fire on:

- Negation cases: `I didn't need help`, `I almost called for help`,
  `I no longer need help`, `I'm not asking for help`.
- Word-boundary collisions: `helpless`, `helper`, `helpers`. The matcher
  requires trigger tokens to be at word boundaries.
- Smart-apostrophe negation forms — the matcher normalizes Unicode first.

If the user input looks safety-adjacent but matches nothing, the dispatcher
logs at WARNING level with the dropped utterance. This is the spec's
"log at WARNING level if a safety request is dropped" requirement.
