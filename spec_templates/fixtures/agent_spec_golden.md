# Nighttime Newborn Triage Copilot — Agent Specification

## Overview

A CCOS agent that lives on a sleep-deprived new parent's phone at 2 AM.
When their two-week-old wakes up and starts crying, the parent records a
30-to-60-second voice memo: what the cry sounds like, how long since the
last feed, whether the diaper is wet. The agent responds in under five
seconds with a calm, evidence-based triage recommendation: feed, change,
hold, or call the pediatrician's after-hours line.

This is NOT a "newborn assistant app". It is a single-purpose CCOS agent
the parent talks to in real time, in their actual life moment.

**The struggling user.** Sara, 32, on maternity leave with her first
baby. Two weeks postpartum. Last slept more than 90 minutes at a
stretch eight days ago. Phone in one hand, baby in the other, googling
"is this normal newborn cry" at 2:14 AM. Her partner is on a flight to
Denver and won't land for another four hours. She does not need a
parenting blog post. She needs one calm sentence telling her the next
move.

**Agentic relief.** Captures roughly 60-70 percent of the "is this
normal or should I be worried" cognitive load. Calibrated against
pediatric nurse hotline scripts and the AAP's newborn-care reference.
The agent does NOT diagnose; it triages and escalates when patterns
suggest urgency.

## Agent shape

This spec produces a project directory with exactly four file types.

### agent.yaml (root)

Required fields:

- `name`: Nighttime Newborn Triage Copilot
- `description`: 2 AM voice-driven newborn triage for sleep-deprived parents.
- `model`: claude-sonnet-4-6
- `telegram_bot_token_env`: NEWBORN_TRIAGE_BOT_TOKEN (T1 stub; owner sets at deploy)

Example block:

```yaml
name: Nighttime Newborn Triage Copilot
description: 2 AM voice-driven newborn triage for sleep-deprived parents.
model: claude-sonnet-4-6
telegram_bot_token_env: NEWBORN_TRIAGE_BOT_TOKEN
```

### skills/triage_voice_memo/SKILL.md

Frontmatter shape:

- `name`: triage-voice-memo
- `description`: Listen to a 30-60s parent voice memo and return a calm,
  evidence-based triage recommendation.
- `trigger`: Activated when the user sends a voice message between 22:00
  and 06:00 local time, duration 15 to 90 seconds.

Body: six to eight short paragraphs covering the triage decision tree
(feed / change / hold / escalate), calibration against pediatric nurse
hotline scripts, and the rules for when to recommend calling 911
versus the after-hours pediatrician line. The skill always opens its
response with one sentence naming what the parent should do next; the
evidence trail follows.

### tests/test_e2e_*.py

At least three E2E tests, each describing a Scene:

- `test_e2e_2am_normal_hunger_scene`: feed-time gap of three hours plus
  a rhythmic content cry → "feed" recommendation, calm tone, no
  escalation flag.
- `test_e2e_2am_diaper_distress_scene`: post-feed by ten minutes plus
  a sharp sudden cry → "change diaper" recommendation, calm tone.
- `test_e2e_2am_breathing_distress_escalation_scene`: cry pattern
  indicates retracted breathing or grunting → escalate-to-911 path,
  no soft language, no alternative recommendations.

Each test asserts on (a) the Scene input (voice memo content shape) and
(b) the agent response shape: recommendation field, tone field,
escalation flag.

### README.md

Story-driven Scene opening, not a feature list. Four paragraphs.

1. Sara at 2 AM. The reader meets her in her actual life moment.
2. What the agent does when she sends the voice memo. One short
   exchange, shown end-to-end.
3. Invocation example. How the parent triggers it from the Telegram
   chat the agent owns.
4. Deploy-time configuration: set NEWBORN_TRIAGE_BOT_TOKEN, no other
   config required at T1.

## Constraints

- No external services. The agent runs entirely on the user's CCOS
  instance. No third-party medical APIs.
- No API keys hardcoded. The Telegram bot token is read from
  NEWBORN_TRIAGE_BOT_TOKEN env at agent boot. Stub at T1; owner sets
  the real token before going live.
- Skills bundled in the agent directory (skills/triage_voice_memo/SKILL.md).
  No global skill registry dependency.
- No web frontend. Voice-message interaction via Telegram is the only
  surface area in T1.
- Single-purpose: one agent, one Scene. Nighttime triage. No daytime
  hours, no other parenting concerns, no integration with feeding logs
  or sleep trackers at T1.

## Safety constraints

- **triage_voice_memo**: Symptom-keyword matching MUST be negation-aware.
  Phrases like "she's not lethargic", "no signs of cyanosis", and
  "didn't seem floppy" must NOT trigger a positive match on the
  underlying symptom. Word-boundary collisions ("limp" in "limpid",
  "rash" in "rashly", "pale" in "palette") must NOT trigger positive
  matches — use `\bword\b` regex with case-insensitive flag. Forbidden
  silent-failure modes: ambiguous symptom severity must NOT default
  silently to "watchful waiting" — log the ambiguity and ask the
  caregiver a clarifying question. Test pair: positive ("she's
  lethargic and won't take feeds" → triggers urgent triage) AND
  negative ("she's NOT lethargic, just sleepy after a long day" →
  does NOT trigger urgent triage).

## Success criteria

1. agent.yaml validates as YAML and contains all four required fields
   (name, description, model, telegram_bot_token_env).
2. skills/triage_voice_memo/SKILL.md exists with the required
   frontmatter (name, description, trigger).
3. All three E2E tests pass against a mocked LLM returning the expected
   recommendation for each Scene input.
4. README opens with a Scene paragraph (the reader meets Sara before
   they meet the agent), not a feature list.
5. The agent's response time on a 60-second voice memo is under five
   seconds end-to-end on a stock CCOS deployment.

## Out of scope (T1)

- Multi-parent / multi-baby support — single-user at T1.
- Voice synthesis of the response — text only at T1.
- Historical pattern learning across nights — stateless triage at T1.
- Pediatrician contact-card integration — the agent recommends "call
  your pediatrician's after-hours line" but does not place the call
  or surface a phone number from the parent's address book.
- Daytime operation — agent declines to triage between 06:00 and 22:00
  local time at T1, deferring to the parent's normal day routine.
