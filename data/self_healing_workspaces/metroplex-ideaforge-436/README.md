# Elder-Care Safety & Support Companion

## Scene

It is 2 AM and Sarah is sitting on her bedroom floor with the door wedged shut. She is 35. Her mother, 65, has had borderline-personality traits for as long as Sarah can remember, but tonight was different — a Mother's Day argument over the dinner table turned into a thrown plate, then a vase, then a glass that missed her head by inches. Sarah has not slept. She is afraid to leave the bedroom. She is also afraid to call anyone, because what kind of daughter calls the police on her own mother on Mother's Day? She is exhausted, vigilant, and quietly disintegrating in her own house.

Into that quiet, the Elder-Care Safety & Support Companion does one small thing well: it helps Sarah write down what just happened. "I'd like to document the incident," she types into her phone, and the agent answers calmly — without judgement, without diagnosis — that the incident has been logged with a category and a timestamp she can take to her primary-care team, her therapist, or a social worker when she's ready. The agent does not push her to call 911. It does not tell her she is a bad daughter. It records what she says, the way a friend with a clipboard would, while she catches her breath.

The agent is invokable through CCOS with `claude --agent Elder-Care_Safety_Support_Companion`. From there Sarah can say "document incident", "I need help", or any of the lay-register phrasings the agent understands ("write this down", "I'm scared", "call somebody"). The agent will respond with either a structured incident record or a calm Safety Protocol — three or four ordered steps and a small list of protective-resource phone numbers (Adult Protective Services, the National Domestic Violence Hotline, 988, her local non-emergency police line). Multiple skills can fire on a single utterance — "I need help and want to document what happened" returns both responses.

Before the first deployment, configure the Telegram bot token by setting the `ELDER_CARE_BOT_TOKEN` environment variable on the CCOS host. The agent.yaml references the env var name, never the literal token — the spec requires that no secrets be hardcoded. After the token is in place the agent is fully self-contained: no external HTTP services, no calendar integration, no cross-session memory, and no voice synthesis (all explicitly out of scope for T1). Sarah's data stays on her CCOS instance.

## Running the tests

```bash
python -m pytest tests/ -v
```

## What's out of scope (T1)

- Multi-user partitioning
- Voice synthesis
- Cross-session memory
- Web frontend
- Calendar integration
- External HTTP services
- Insurance claim submission

These will not be added in this version. The agent is intentionally single-purpose.
