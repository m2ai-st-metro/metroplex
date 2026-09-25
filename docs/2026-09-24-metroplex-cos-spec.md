# Metroplex CoS: intake, delegation and routing over Teletraan

Date: 2026-09-24. Status: DRAFT spec for Matthew's review. Nothing built, activated, or
migrated by this document.

Decision record: `~/vault/decisions/2026-09-24-metroplex-cos-router.md` (decisions 1 to 7).
Parent plan: `~/projects/agents/claudeclaw-os/docs/plans/2026-09-12-0128-feat-agent-organization-continuity-plan.md`
(R1 to R12, AE1 to AE6) and `2026-09-21-agent-organization-pair-spec.md` (AE7 to AE9).
Pilot code: branch `feat/organization-continuity-pair`, local commits ccos `8de84f13`,
teletraan `e9c9edd`, t3 `7d45d5163` (Astra-approved as an isolated pilot only).

## Objective and done-state

Matthew sends an idea to `@m2ai_metroplex_bot`, answers at most one question, taps "yes" on
one objective card, and the project then progresses through Teletraan without per-task
approval. Metroplex decides assignment, recruitment, assistance, and escalation for every
granted project by reacting to durable Teletraan wakes. Matthew is contacted only for
unresolved intent, authority outside the grant, or a reserved action.

Done when the acceptance examples in this spec (C1 to C6, plus AE3, AE5, AE6 re-proven) pass
on durable evidence in an isolated environment, reviewed by an independent reviewer.

## Verified current state (read 2026-09-24, this session)

Metroplex (`~/projects/st-metro/metroplex`, Python):
- About 16.7k lines of Python in top-level modules, `gates/` and `adapters/`; last commit `4923418` (2026-08-12).
- `systemctl --user` unit `metroplex` is inactive and disabled; its crons were retired 2026-08-18.
- Built as an idea-to-publish pipeline: gates triage, build, review, publish (`gates/`), a
  dispatcher that writes to `~/projects/claudeclaw/store/claudeclaw.db` (path no longer exists),
  and build adapters for the self-healing daemon (deliberately down) and Oz.
- `notifier.py` `TelegramNotifier` sends only. There is no inbound bot handling.
- Model calls: `gates/llm_expander.py:690` uses an OpenAI-compatible client against
  `https://api.deepinfra.com/v1/openai`. `anthropic` appears in `requirements.txt` but no module
  imports it. (This corrects the earlier review claim that Metroplex calls the Anthropic SDK.)
- `safety.py`: `CircuitBreaker` (typed to gates triage/build/publish), `CycleCaps`,
  `BudgetEnforcer`, `ShutdownHandler`. `METROPLEX_TELEGRAM_BOT_TOKEN` exists in `~/.env.shared`.

Teletraan pilot (`~/projects/worktrees/teletraan-continuity-pair`):
- `src/api/work-service.mjs`: authenticated Unix socket, principals resolved from provisioned
  credentials (never caller-supplied). Actions: `command`, `snapshot`, `history`,
  `attempt_check`, `dispatch_reconcile`, binding and runtime-config actions.
- `src/domain/work.mjs`: append-only `work_events` (unique `command_id`, idempotent replay)
  projected into `work_objects`. Kinds include `project`, `grant`, `task`, `contribution`,
  `attempt`, `judgment`, `wake`, `usage`.
- Wakes are already durable in Teletraan: `wake(task, reason)` writes a `wake` object with an
  incrementing `cycle` on task create, scope change, status change, attempt result, stop, and
  import. `wake.ack` and `wake.checkpoint` operations exist; stale cycles fail `STALE_WAKE`.
- `project.create` requires the `operator` principal and auto-issues a grant
  (`work`, `assign`, `review`) to the project owner. `project.member` adds members and grants.
- Projects carry optional `concurrencyLimit` and `providerCallLimit` (counts, not dollars).
  `usage` objects record observed provider cost as telemetry (`costSource`). The pilot work
  model has no required budget fields. Live V1 (`teletraan-core`, `validateAdmission`) still
  requires `costBudgetUsd` and `timeBudgetSeconds`.

CCOS pilot (`~/projects/worktrees/ccos-continuity-pair`):
- `src/orchestration-wakeups.ts` (72 lines): `OrchestrationWakePump` polls `snapshot`, picks
  unacknowledged wakes, runs one orchestrator turn per project. It is a poller, not a store.
- `src/project-orchestrator.ts` (181 lines): the reasoning turn.

Consequence: decision 1 (durability in Teletraan) is already true for wakes in the pilot.
What Metroplex replaces is the CCOS poller and the orchestrator turn, about 250 lines.

## Roles

```
 MATTHEW        intent; one "yes" per project; reserved actions
 METROPLEX      CoS: intake, objective cards, delegation, routing, briefings
 TELETRAAN      single durable authority: projects, grants, tasks, wakes, events
 DATA           do-it-now Telegram assistant; never routes (CLAUDE.md updated 2026-09-24)
 T3 / NATIVE    execution: enrolled T3 threads; local and OpenRouter/DeepInfra tool loops
 JEV            typed judgments only; no tools, no grants, no acceptance
 CCOS           views (org/project/task, Mission Control); caches, never truth
```

## Architecture

```
 @m2ai_metroplex_bot                              @m2ai_data_bot
        |                                               |
        v                                               v
 +---------------------------+                    DATA (do it now,
 | METROPLEX (Python daemon) |                     never routes)
 |  intake     card store    |
 |  turn runner  safety      |
 |  briefing   audit log     |
 +------+-------------+------+
        | cos creds   ^ snapshot poll (unacked wakes, cursor)
        v             |
 +===================================================+
 | TELETRAAN work service (pilot work-service.mjs)   |
 |  events -> objects: project grant task wake ...   |
 +======+==================+=========================+
        | dispatch         | events
        v                  v
   T3 threads /       CCOS views,
   native runtime     Matrix writer (P2)
```

## Path 1: intake to granted project

```
 YOU -> bot: "idea: ..."
   |
   v
 I1 classify: new objective | belongs to project P | note
      note      -> Teletraan proposal (inert, /quick shape), reply 1 line
      project P -> task.create under P's grant, goes to Path 2
      objective -> I2
 I2 at most one clarifying question (only for unresolved intent)
 I3 objective card (stored in Metroplex card store, hashed)
 I4 bot shows card with [Yes] [Edit] [Drop]
   |
   v  YES from Matthew's Telegram user id, in Matthew's chat, matching card hash
 I5 project.create (see T1) + root task.create for the owner role
 I6 reply: project id + owner, "running"
```

Objective card fields: `title`, `objective`, `done_when`, `scope` (in/out), `owner` (role
agent), `reserved_actions` (default list below), `hosted_allowed` (bool),
`concurrency_limit`, `kill` (condition that ends the project). No budget field (decision 6).

Default reserved actions (decision record, Matthew had no preference): publish/push/deploy,
external contact, live fleet config.

## Path 2: delegation and routing

```
 Teletraan wake (created | scope | status | result | stopped | imported)
   |
   v  Metroplex poll: snapshot, unacknowledged wakes, one job per project
 R1 gate: paused? breaker open? cycle cap hit? in schedule window?  -> hold, log
 R2 turn: read live project/task/attempt state + wake.priorDecisions
          optional Jev judgment (judgment.record)
 R3 decide exactly one: assign | recruit | assist | wait | escalate
 R4 issue commands with fresh command IDs and expected revisions
 R5 wake.ack (cycle-fenced) ; record decision + reason in audit log
   |
   v
 escalate only for: unresolved intent | action outside grant | reserved action
   -> task.block with reason, bot message (3 lines), unrelated work continues
```

Turn contract: a turn reads only Teletraan state and the wake; it never trusts memory for
status. It may call only the work operations the `cos` principal is granted (T1). A crash
between R4 and R5 is safe: commands are idempotent by `command_id`, and an unacknowledged
wake is retried; a changed cycle fails `STALE_WAKE`.

## Required Teletraan changes

- **T1. `cos` principal with a narrow operation set.** Credentials provisioned like other
  principals. Allowed: `project.create` only with an approval record (T2), `task.create`,
  `task.assign`, `task.block`, `task.scope`, `agent.recruit`, `project.member`,
  `contribution.create`, `wake.ack`, `wake.checkpoint`, `judgment.record`, `snapshot`,
  `history`. Not allowed: runtime config, bindings, `grant` changes other than via
  `project.create`, `migration.activate`, anything reserved. Metroplex never holds `operator`.
- **T2. Approval record on `project.create`.** New `approval.record` (cos) storing card hash,
  Telegram `chat_id`, `from.id`, `message_id`, timestamp. `project.create` by `cos` must cite an
  unused approval whose hash matches the submitted card and whose `from.id` equals the
  configured Matthew id. Honest limit: Metroplex's bot process attests the Telegram fields, so
  this is an audit trail plus a second check, not cryptographic proof of Matthew.
- **T3. Reserved actions as data.** Project field `reservedActions`. Enforcement is capability
  absence (workers get no forge, messaging, or fleet credentials, per the existing worker
  contract) plus `teletraan-publish` for publication. Teletraan rejects any operation tagged
  with a reserved category from a non-operator principal.
- **T4. No budget requirement in the new model.** Keep the pilot's count limits. Decide P3 on
  whether `usage` cost telemetry stays.
- **T5. Wake query.** Add `wake_pending` (unacknowledged wakes since a cursor) so Metroplex
  does not pull a full snapshot every poll.

## Metroplex changes

Keep: `safety.py` (generalize `CircuitBreaker` gates to `intake`, `turn`, `briefing`),
`CycleCaps`, `ShutdownHandler`, `audit.py` (`decisions.log`), `notifier.py`, `health.py`
checks that still apply, `deploy/metroplex.service` (`Restart=always`).

Retire (move to a cold archive tag, remove runtime entrypoints): `gates/` (triage, build,
review, publish, readme, quality_scorer, readiness, llm_expander), `adapters/`,
`dispatcher.py`, `readers/`, `oz_bridge.py`, `build_adapter.py`, `feasibility_scorer.py`,
`quality_ratchet.py`, `postmortem.py`, `BudgetEnforcer`, the self-healing restart script,
the project-scoped `.claude/skills/self-healing-daemon` skill.
Rewrite the README and CLAUDE.md charter from "closes all human gates" to "closes routine
gates; reserved actions stay Matthew's."

Add:
- `bot_intake.py`: long-poll `getUpdates` for the Metroplex bot, restricted to Matthew's chat
  and user id; inline-button callbacks for card Yes/Edit/Drop.
- `cards.py`: card store (SQLite, `data/metroplex.db`), hashing, lifecycle.
- `teletraan_client.py`: work-socket client (JSON over Unix socket, `cos` token).
- `turn_runner.py`: model-agnostic turn with a provider adapter (M1).
- `briefing.py`: batched digest of completions and escalations to the bot.

M1, model access for turns (decision needed, see Open questions): the Anthropic API key is
disabled. Options are the Claude CLI subprocess authenticated by `CLAUDE_CODE_OAUTH_TOKEN`
(depends on proposal `Q-20260924-181517-agent-oauth-env-token`), DeepInfra (already wired in
Metroplex), or local Qwen. Adapter interface first; default chosen at review.

## Proposals for open items (accept or strike)

- **P1. Owner, sink, kill, pause.** Owner: Matthew. Sink: Metroplex bot chat plus
  `data/decisions.log`. Kill: `/pause` on the bot sets a global pause (no new turns, running
  attempts continue); `/stop` also stops the daemon; any breaker at threshold 3 halts its loop
  and messages once. Per-cycle caps: max 3 turns per project per cycle, max 1 concurrent turn
  per project. Without budgets, these are the runaway guard.
- **P2. Matrix.** One writer: Metroplex, on task completion or cancellation, writes one
  fleet_memory entry per task with outcome, key decision, and any failure lesson. No writes for
  intermediate events. Agents do not write task events to Matrix.
- **P3. Cost telemetry.** Strike `usage.cost` recording entirely (decision 6), or keep it as
  passive telemetry never used for gating. Recommend strike, to honor "not something I'm going
  to track."

## Acceptance examples

Re-proven from the parent plan against the Metroplex poller and turn (replacing CCOS
`OrchestrationWakePump` and `project-orchestrator.ts`):
- AE3: busy owner gets a contributor; a slow but progressing worker is not replaced.
- AE5: paused or cancelled task launches nothing new; unrelated tasks continue.
- AE6: Metroplex killed mid-dispatch recovers from unacknowledged wakes with no duplicate
  worker and no question to Matthew.

New:
- C1: idea to bot, one question, Yes: project and root task exist, grant held by the owner,
  approval record cited, reply received.
- C2: no Yes (Drop or silence): no project, no grant, no task. Card expires after 7 days.
- C3: a Yes from any other Telegram user or chat, or for a stale card hash: `project.create`
  rejected, logged.
- C4: a worker needs a reserved action: task blocks with a 3-line bot message; unrelated
  tasks keep progressing.
- C5: `/pause`: no new turns; resume drains pending wakes once, without duplicates.
- C6: an idea sent to Data produces at most an inert proposal, never a task or project.

## Units and order

1. U1 Teletraan T1, T2, T5 on the pilot branch, with focused tests.
2. U2 Metroplex strip: archive tag, remove retired entrypoints, charter rewrite.
3. U3 `teletraan_client.py` + poller + safety generalization (AE6, C5 first).
4. U4 `turn_runner.py` + provider adapter (AE3, AE5).
5. U5 `bot_intake.py` + `cards.py` (C1, C2, C3, C6).
6. U6 T3 reserved actions + escalation path (C4).
7. U7 `briefing.py` + P2 Matrix writer if accepted.
8. U8 workspace doctrine rewrite in the same release that grants go live:
   `~/.claude/rules/external-llm-dispatch.md`, `~/.claude/rules/displace-dont-add.md`
   (Teletraan admission wording), `~/CLAUDE.md`, `~/AGENTS.md`.
9. U9 integrated pilot run and independent review.

Displaces: CCOS `OrchestrationWakePump` and `project-orchestrator.ts` (pilot), Data's routing
duties (removed 2026-09-24), the 2026-07-20 Data-as-router decision (superseded), Metroplex's
gate pipeline. Nothing new is added beside Teletraan as a task store.

## Out of scope

Production rollout; migration of the 50 V1 tasks and 10 unadmitted proposals (decision 7,
Metroplex proposes the grouping after it is live); Paperclip archive import; the CCOS Mission
Control Teletraan tab (proposal `Q-20260924-174933-ccos-teletraan-tab`); the agent OAuth token
fix (proposal `Q-20260924-181517-agent-oauth-env-token`); T3 JJ build rebase (pin stays
`t3code-teletraan`).

## Open questions

1. M1: default model for Metroplex turns (Claude CLI with OAuth env token, DeepInfra, local Qwen).
2. P1, P2, P3: accept, edit, or strike.
3. Card expiry: 7 days proposed.
4. Where the pilot Teletraan work service runs for the isolated Metroplex pilot (separate state
   dir and socket from live `teletraan.service`), confirm before U1.
