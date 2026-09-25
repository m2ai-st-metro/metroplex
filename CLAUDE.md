# CLAUDE.md: Metroplex

Metroplex is Matthew's Chief of Staff over Teletraan: intake, delegation and routing.
It closes routine gates; reserved actions stay Matthew's. It is a thin, replaceable judge:
durable work state lives in Teletraan, and Metroplex only reads it, decides, and issues
fenced, idempotent commands as the `cos` principal.

The gate pipeline (triage, build, review, publish) was retired 2026-09-24. Its code is
preserved at git tag `archive/gate-pipeline-2026-09-24`. Do not restore it.

## Design documents (read before changing behavior)

- Decisions: `~/vault/decisions/2026-09-24-metroplex-cos-router.md`
- Spec: `docs/2026-09-24-metroplex-cos-spec.md`
- Decision layer plan (Fable): `docs/2026-09-24-metroplex-decision-layer-plan.md`
- MVP scope (what is built now): `docs/2026-09-24-metroplex-mvp-scope.md`

## Invariants

- A project exists only from Matthew's one-tap yes on a card whose hash he approved.
  Teletraan enforces this (`approval.record`, `project.create` by `cos`).
- No budget, cost, or provider-call-limit tracking. Runaway guards are pause/stop,
  circuit breakers, and per-cycle caps (`cos/safety.py`).
- Jev (TypeSafe hosted API) is evidence only. It never issues commands, holds grants,
  or accepts work. Projects with `hostedAllowed: false` never call Jev.
- Metroplex never accepts or completes work. Owners do.
- Intake comes only from `@m2ai_metroplex_bot`, restricted to Matthew's user id. Data
  (the CCOS Telegram agent) does not route.

## Layout

| Path | Purpose |
|---|---|
| `cos/config.py` | Environment configuration, v1 thresholds and caps |
| `cos/teletraan_client.py` | Work-service client (Unix socket, JSON, half-close) |
| `cos/spec.py` | Card and task spec validation; `card_digest` matches Teletraan |
| `cos/safety.py` | Circuit breakers, cycle caps, shutdown |
| `cos/store.py` | Local state: cards, breakers, counters, pause (never work state) |
| `cos/jev_client.py` | Jev judgments (port of the pilot jev-adapter); evidence only |
| `cos/reasoning.py` | Reasoning turns via OpenAI-compatible endpoints; no hardcoded model |
| `cos/routing.py` | Path 2: one turn per wake (candidates, Jev, fallback, assign, escalate) |
| `cos/motion.py` | Sweeps M1-M5 that raise fenced wakes; never act directly |
| `cos/intake.py` | Path 1: idea, card, yes, decomposition; reserved-action approvals |
| `cos/bot.py` | `@m2ai_metroplex_bot` transport, approver-only |
| `cos/daemon.py` | One loop: bot poll, wake routing, sweep, card expiry, pause/stop |
| `metroplex.py` | CLI: run, status, pause, resume, reset, sweep --dry-run |
| `notifier.py`, `audit.py`, `event_emitter.py` | Kept from the gate era, standalone |

## Running (isolated pilot only until Matthew approves live use)

Required env: `METROPLEX_TELEGRAM_BOT_TOKEN`, `METROPLEX_TELEGRAM_CHAT_ID`,
`METROPLEX_APPROVER_IDS` (Matthew's Telegram user id), `METROPLEX_WORK_SOCKET`,
`METROPLEX_COS_TOKEN_FILE` (the `cos` credential for the Teletraan work service, which
must also set `TELETRAAN_WORK_APPROVER_IDS`), `TYPESAFE_API_KEY`, `DEEPINFRA_API_KEY`.
Reasoning model: `METROPLEX_REASONING_MODEL`, else the existing `METROPLEX_SPEC_LLM_MODEL`.

## Testing

```bash
venv/bin/python -m pytest tests/ -q
```

Integration tests start a real Teletraan work service from the pilot checkout
(`METROPLEX_TELETRAAN_ROOT`, default `~/projects/worktrees/teletraan-continuity-pair`)
on a temp socket, and skip when node or the checkout is missing.
