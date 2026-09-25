# Metroplex Decision Layer Plan (Jev routing, concept-to-spec, motion, Matrix/Sky-Lynx)

Status: PLAN ONLY. Nothing built, committed, or activated. Written 2026-09-24 by Fable 5.1 (read-only recon).

Sections are appended incrementally. Recon log first, then sections 1 through 10.

## 0. Recon log (what was actually read)

(appended as files are read)
Read so far (all via cat -n, this session):
- metroplex/docs/2026-09-24-metroplex-cos-spec.md (full), vault/decisions/2026-09-24-metroplex-cos-router.md (full)
- teletraan-continuity-pair/src/domain/work.mjs (500 lines, full), src/api/work-service.mjs (108, full)
- jev-playground/server/jev.ts (158), shared/types.ts (91), shared/aggregate.ts (76)
- ccos-continuity-pair/src/orchestration-wakeups.ts (72), project-orchestrator.ts (181), agent-engine/jev-adapter.ts (162), jev-tool.ts (104)
- metroplex: CLAUDE.md (1-150), safety.py (302, full), audit.py (56, full), notifier.py (1-80), health.py (defs + 60-153), config.py (grep), event_emitter.py (1-70)
- rules/fleet-memory-doctrine.md (full)
- sky-lynx: CLAUDE.md (1-80), README (1-80), src/sky_lynx/ listing, analyzer.py (defs + 195-330), trigger_listener.py (1-60), mission_reader.py (1-40), proposal_tracker.py (86-239 via grep + 86-186), metroplex_reader.py (grep), claudeclaw_writer.py (1-25), auto_applicator.py (grep)
- matrix: CLAUDE.md (1-39), README (1-25)
- teletraan-core: README (full, 7 lines), src/domain/contracts.mjs (1-45), bin/ listing
- claudeclaw-os/docs/plans: 2026-09-12 continuity plan (headings, R1-R12 at 56-78, AE1-AE6 at 81-86, KTD1-KTD8 at 127-134), 2026-09-21 pair spec (117 lines, full)
- archives/paperclip-exit-20260909: top-level listing, historical-config/ listing, paperclip-cli-SKILL.md (marker only, 14 lines), crontab-pre-removal.txt (grep), build-disposition-ledger.py (1-90), removal-manifest.json (head)
- memory: paperclip-m2ai-chassis.md, paperclip-retired-2026-09-09.md, feedback_paperclip_board_is_live_fleet_surface.md, st-metro-loops-not-megafactory.md, sky-lynx-cadence-upgrade.md (all full)
- jev-playground: README (1-70), shared/presets.ts (grep only)

Not read (out of scope or not present): the shared env file; `archives/paperclip-exit-20260909/encrypted/`; the Paperclip portable export tables (they live under `~/content/m2ai/financial-independence/subgoals/paperclip-exit/`, outside the allowed list, per build-disposition-ledger.py:14-15); `sky_lynx/` at the repo root does not exist, the package is `src/sky_lynx/`; jev-playground `server/index.ts`; the 2026-09-21 pair handoff; T3 sources. Claims about those are marked as unverified where they appear.

RCL: Matthew confirmed mid-plan that RCL means "recursive continuous learning". Section 6 is designed around that definition. It is not an open question.

## 1. Decision inventory

Conclusion: Metroplex makes 24 distinct decisions in seven groups. Only 6 need a full reasoning turn (I1 classify-with-question, S2 decompose, R2 assign/recruit/assist/wait, E1 escalate-or-continue, Q1 result triage, L2 lesson phrasing). Everything else is deterministic against Teletraan state. Jev is consulted on 8 of them, always as evidence, never as the actor. Matthew can override every decision through Teletraan operator commands or the bot; Metroplex never overrides Matthew.

Legend: Jev = judgment type consulted (none | noul | choice | score). Turn = reasoning turn needed (yes) or deterministic (det). Override = who can reverse it and how.

### 1A. Intake (bot channel only, spec Path 1)

| ID | Decision | Trigger | Reads from Teletraan | Writes (command) | Jev | Turn | Override |
|---|---|---|---|---|---|---|---|
| I1 | Classify message: note, belongs-to-project P, new objective | bot message from Matthew's user id | snapshot: projects (titles, objectives) visible to `cos` | none yet | choice (labels: `note`, one per open project, `new_objective`) | yes when Jev is low-confidence, det otherwise | Matthew: reply `/reclass <id> <label>` |
| I2 | Ask at most one clarifying question or not | I1 = new_objective and `done_when` cannot be drafted | none | none (bot message) | noul "Is intent resolved enough to write done_when?" | yes to draft the question | Matthew answers or taps Drop |
| I3 | Draft the objective card | I1 = new_objective (after I2) | snapshot: agents (roles, capabilities) for owner choice | none (card store, hashed) | score "card completeness 0-4" as a self-check before showing | yes | Matthew: Edit |
| I4 | Accept a Yes as valid | callback from Telegram | none | `approval.record` (T2), then `project.create` + root `task.create` | none | det | Matthew: `/drop <card>` before Yes; operator `task.cancel` after |
| I5 | Sub-concept inside a granted project | I1 = belongs-to-project P | project P grant for `cos`, P.status, card goals | `task.create` under P citing `cardGoalId` (D9) | noul "fits P's objective and scope-in?" | det when noul >= threshold, else route to I2 | Matthew: `/drop`; owner `task.cancel` |

### 1B. Spec and decomposition (runs once per granted project, and once per new sub-concept)

| ID | Decision | Trigger | Reads | Writes | Jev | Turn | Override |
|---|---|---|---|---|---|---|---|
| S1 | Card is complete enough to grant (all schema fields present, `done_when` observable, owner exists) | I3 output | agents list (owner must be a persistent agent, work.mjs:185) | none; blocks I4 until complete | score "done_when observability 0-3" | det (schema) + score | Matthew: Edit |
| S2 | Decompose objective into goals, then tasks with checkpoints and quality checks | `project.create` receipt (wake reason `created` on root task, work.mjs:243) | project, root task, agents | `task.create` per leaf (D1 fields), `task.dependencies` (work.mjs:248-253) | none | yes (one turn, bounded to <= 12 tasks per pass, P1 caps) | Owner agent: `task.scope`; Matthew: `task.cancel` |
| S3 | Assign checkpoints and quality checks to each task | inside S2 | spec templates (Sky-Lynx accepted proposals, section 6) | part of `task.create` payload | none | det from template + S2 output | Owner: `task.scope` |
| S4 | Task spec passes uniformity lint before creation | any `task.create` by `cos` | none | reject locally, log `spec_lint_fail` | none | det | none needed; lint is the gate |

### 1C. Routing and assignment (spec Path 2, R1 to R5)

| ID | Decision | Trigger | Reads | Writes | Jev | Turn | Override |
|---|---|---|---|---|---|---|---|
| R1 | Build candidate set for a task | wake `created`, `scope`, `stopped`, `imported` | project.members, grants (`work` active, work.mjs:290-291), agents (capabilities, lifecycle, status), live attempts per agent, concurrencyLimit (work.mjs:287), bindings availability | none | none | det | n/a |
| R2 | Choose exactly one: assign, recruit, assist, wait, escalate | after R1 | R1 output + wake.priorDecisions (work.mjs:155) + contributions/attempts on the task + Matrix prior-outcome hits | see R3/R4 | choice over candidates plus `recruit`, `wait`, `none_fit` labels (section 2) | yes when Jev low-confidence/unavailable; det when Jev is confident and the choice passes the deterministic guards | Owner agent: `task.assign` back (needs `assign` grant); Matthew: operator |
| R3 | Write the handoff text | R2 = assign | task, checkpoint (work.mjs:357), prior attempts | `task.assign` with `handoff` (work.mjs:244-247), `contribution.create` (work.mjs:274-283), `attempt.queue` (work.mjs:284-315) | none | yes (short) | Owner: `task.assign` |
| R4 | Recruit a temporary specialist | R2 = recruit | agent capability gaps, project.hostedAllowed | `agent.recruit` (work.mjs:168-173) then R3 | none | det once R2 decided | Owner/Matthew: `agent.retire` |
| R5 | Runtime and model for the attempt | R3 | project.hostedAllowed, binding/runtimeConfig pins (work.mjs:299-310), Sky-Lynx accepted routing proposals | `attempt.queue.runtime/model` | none | det (table lookup) | Matthew via proposal accept/reject |
| R6 | Reverse a recent allocation | wake `result`/`stopped` on a task with a prior decision < N hours old | priorDecisions, progressAt vs heartbeatAt | usually `wait` | none | det: refuse unless materially changed evidence (KTD7) | Matthew |

### 1D. Motion and health (section 4 has cadence)

| ID | Decision | Trigger | Reads | Writes | Jev | Turn | Override |
|---|---|---|---|---|---|---|---|
| M1 | Attempt is stalled (heartbeat fresh, no progress) | motion sweep | attempt.heartbeatAt (work.mjs:369), progressAt (work.mjs:356), contribution.checkpoint | `wake.raise stalled` (D10) then R2 with `assist` favored | none | det | Owner |
| M2 | Attempt is dead (no heartbeat) | motion sweep | heartbeatAt age, binding state | `wake.raise stalled`; never `attempt.stopped` (runtime-only proof, work.mjs:372-374) | none | det | runtime reconcile |
| M3 | Ready task has no contribution | motion sweep | tasks status `ready` with zero contributions, deps done | `wake.raise unassigned` then R2 | none | det | Owner |
| M4 | Orphaned wake (unacked past age, or acked without decision) | motion sweep | wakes, cycles | re-enter turn; if cycle changed, drop (STALE_WAKE) | none | det | n/a |
| M5 | Blocked task can unblock (its reason cites a task now `done`, or Matthew answered) | motion sweep, bot reply | blocked tasks + reasons, dependencies | `task.resume` (work.mjs:262) | none | det | Owner |
| M6 | Project is terminal but temporary agents remain | wake `done` | agents lifecycle temporary + active | `agent.retire` (work.mjs:174-183) | none | det | Owner |
| M7 | Hold: paused, breaker open, cycle cap hit, outside schedule window | every poll | Metroplex local state (safety.py) | none, log | none | det | Matthew `/resume`, `metroplex reset` |
| M8 | Provider allowance near cap | motion sweep | project.providerCalls vs providerCallLimit (work.mjs:199) | bot note at 80 percent, `task.block` reason `allowance` at 100 percent on the next wake | none | det | Matthew raises limit (operator) |

### 1E. Quality and acceptance

Metroplex never accepts. `contribution.accept` and `task.complete` require the task owner or operator (work.mjs:380, 386). Metroplex nudges owners and records evidence.

| ID | Decision | Trigger | Reads | Writes | Jev | Turn | Override |
|---|---|---|---|---|---|---|---|
| Q1 | Result triage: looks acceptable, needs rework, or needs owner judgment | wake `result` (work.mjs:353) | attempt.result, task.acceptance, task.qualityChecks (D1), contribution.checkpoint | `judgment.record` + owner nudge via `task.assign` handoff "review result", or `contribution.create` for rework | score "acceptance evidence 0-3" + noul per quality check | yes when score is mid-band | Owner accepts or rejects |
| Q2 | Quality-check gate before owner nudge | Q1 | qualityChecks list, receipts (work.mjs:364-367) | as Q1 | noul per check ("receipt shows check X ran and passed?") | det per check | Owner |
| Q3 | Task can complete (deps done, children done, contributions accepted) | wake `result` after owner accept | work.mjs:387-391 preconditions | nudge owner to `task.complete`; Metroplex cannot | none | det | Owner |
| Q4 | Attempt result implies a reserved action is needed next | Q1 | result text, task.reservedAction (D6) | `task.block` reason `reserved:<category>` + bot 3-liner | noul "does next step require publish/contact/fleet-config?" | det when noul is extreme, yes when mid | Matthew answers |

### 1F. Escalation

| ID | Decision | Trigger | Reads | Writes | Jev | Turn | Override |
|---|---|---|---|---|---|---|---|
| E1 | Escalate now, or continue with authorized work | R2 = escalate, Q4, S2 cannot draft done_when, authority outside grant | task, project.reservedActions, grant | `task.block` (reason enum: `intent`, `authority`, `reserved`, `allowance`), bot message | none | yes (must explain the specific need, R12) | Matthew |
| E2 | Message shape | E1 | none | bot: 3 lines, one question, inline buttons where the answer is a choice | none | det template | n/a |
| E3 | Apply Matthew's answer | bot reply/callback | blocked task | `task.scope` (if he changed intent) then `task.resume`; or `task.cancel` | none | det | Matthew |
| E4 | Batch vs immediate | E1 | count of escalations in window | immediate for `reserved`/`authority`; batched into the briefing for `intent` when > 3 in 10 min | none | det | Matthew `/verbose` |

### 1G. Learning (section 6)

| ID | Decision | Trigger | Reads | Writes | Jev | Turn | Override |
|---|---|---|---|---|---|---|---|
| L1 | Emit outcome record | wake `done`, `cancelled`, `stopped` (failed) | task, attempts, judgments, decisions | skylynx-events JSON (event_emitter.py:34-70) | none | det | none |
| L2 | Write a fleet_memory lesson | wake `done` or failed attempt with a distinct cause | attempt.error, result, priorDecisions | Matrix drop-queue item (one per task, P2) | noul "would this change how another agent does a future task?" (doctrine litmus, fleet-memory-doctrine.md:20-22) | yes (short) to phrase the fact | Matthew purge lever (doctrine :39-40) |
| L3 | Extract a routing or spec lesson | L1 stream over a window | events | proposal to Sky-Lynx path (section 6), never self-applied | none | yes, inside Sky-Lynx not Metroplex | Matthew accept/reject |
| L4 | Adopt an accepted Sky-Lynx proposal | proposals.db status `accepted` | proposal row | Metroplex config reload (questionVersion, thresholdVersion, candidate weights, spec template) | none | det | Matthew reject |

## 2. Jev as the routing judge

Conclusion: Metroplex asks Jev one `choice` question per routing wake, over a deterministic candidate set of at most 6 labels plus three fixed escape labels (`recruit`, `wait`, `none_fit`). Jev's answer is evidence stored as a `judgment` object. Metroplex applies the answer only when confidence clears a versioned threshold AND the choice passes the same deterministic guards Teletraan enforces at `attempt.queue`. Anything else falls back to a reasoning turn, and that fallback is itself recorded on the judgment. Jev never issues a command, never holds a grant, and never accepts work.

### 2.1 The real API, as read

- One `POST /v1/systemone` per record; `state` is a string or JSON object, `questions` is a map keyed by id (jev-adapter.ts:23-27; jev README "How Jev is called").
- Three question types: `noul` (returns P(yes)), `choice` (label map with descriptions, returns `choice`, `confidence`, full `probabilities`), `score` (rubric array, returns expected `score`, `confidence`, `probabilities`) (types.ts:47-63; jev.ts:14-34).
- Response validation already exists: answers must match question ids and types, choice labels must be in the criteria set, probabilities must sum to 1 (jev-adapter.ts:28-79). Port this to Python verbatim (D12).
- Limits: 1,200 requests per minute, 32k tokens for state plus the longest question (jev README). The playground paces at max 20 rps (jev.ts:139).
- Hosted: `evaluateJev` throws `HOSTED_JUDGMENT_NOT_AUTHORIZED` unless the project allows hosted processing (jev-adapter.ts:105); the tool returns `fallback: local_reasoning_review` in that case (jev-tool.ts:63-67).
- Low confidence sets `status: needs_review, fallback: reasoning_review`; any error sets `status: unavailable, fallback: reasoning_review` (jev-adapter.ts:131-157). No automatic retries because usage may already be incurred (jev-adapter.ts:93-95).
- Evidence persistence: `judgment.record` with `commandId = judgment:<id>`, `expectedRevision 0` (jev-tool.ts:80-94). Teletraan derives `current` from scopeRevision and task status (work.mjs:487; snapshot at work.mjs:61-64). Stale judgments are returned with `fallback: reasoning_review, status: stale` (jev-tool.ts:98-101).

### 2.2 How Metroplex frames a routing choice

```
 wake (created|scope|stopped|imported|stalled|unassigned) on task T in project P
   |
   v
 R1 candidate builder (deterministic, Python)
   members(P) with active grant.actions incl "work"        (work.mjs:290-291)
   minus agents with live attempt count >= their cap
   minus agents whose writableScope would conflict          (work.mjs:294-298)
   minus retired / temporary-of-other-project agents        (work.mjs:212)
   rank by capability match, then by recent progress ratio, cap at 6
   |
   v
 state = {                                  questions = {
   task: {id, title, objective, acceptance,   route: choice
          checkpoints, qualityChecks,           instructions: "Pick the single best
          writableScope, deps_done, reason},      assignee for this task, or an escape",
   project: {objective, scope_in, scope_out},   criteria: {
   candidates: [{agent, role, capabilities,       "<agentA>": "<role>; <caps>; live=N;
       live_attempts, last_progress_age_s,           last progress 12m ago; 3/3 recent
       recent_outcomes: {done:n, failed:n},          tasks done",
       prior_decisions_on_task: [...]}, ...],      "<agentB>": ...,
   matrix_hits: [{topic, fact}] (<= 3),          "recruit": "no member fits; a temporary
   prior_decisions: wake.priorDecisions             specialist is needed",
 }                                                 "wait": "a live attempt is progressing
                                                       or a dependency is not done",
                                                   "none_fit": "task is malformed or needs
                                                       Matthew (intent/authority)" },
                                            ready: noul "Is this task actionable now?"
                                            }
```

Rules for the frame:
- State is built by code, never by a model, so `inputHash` (jev-adapter.ts:116) is reproducible from Teletraan revisions. The builder records `contextRevision = task.revision` and `scopeRevision`.
- Candidate descriptions carry facts from Teletraan only (attempts, checkpoints, outcomes). No prose from prior model turns. Matrix hits are limited to 3 and labeled as recalled, not observed.
- State budget: trim `recent_outcomes` first, then `matrix_hits`, then candidate count, to stay under 32k tokens. Never trim `task.acceptance` or the escape labels.
- `questionVersion` and `thresholdVersion` are strings from Metroplex config (section 6, L4), sent with every judgment so Sky-Lynx can compare versions later.

### 2.3 Applying the answer (deterministic guard after Jev)

| Jev outcome | Metroplex action | Recorded as |
|---|---|---|
| `choice` = agent label, `confidence >= minConfidence`, `ready.noul >= readyThreshold`, and the agent still passes R1 guards on a fresh snapshot | `task.assign` (handoff cites `judgmentId`), `contribution.create`, `attempt.queue` | `wake.ack decision = "assign <agent> per judgment <id>"` |
| `choice` = `recruit`, confident | reasoning turn to name the specialist, then R4 | `wake.ack decision = "recruit ... per judgment <id>"` |
| `choice` = `wait`, confident | no command; `wake.ack decision = "wait: <reason>"` | as stated |
| `choice` = `none_fit`, confident | E1 escalate path | `task.block reason intent|authority` |
| `status: needs_review` (low confidence) | full reasoning turn with the judgment in context; the turn may agree or differ, and says which | judgment `fallback: reasoning_review`, wake.ack cites both |
| `status: unavailable` (HTTP error, timeout, malformed) | full reasoning turn; no Jev retry in this cycle | judgment `status: unavailable` persisted first (jev-adapter.ts:159-161 rule: persistence failure propagates) |
| `status: stale` (scope changed during the call) | discard for action, keep as history, re-run on the next wake cycle | judgment kept, `current: false` |
| project `hostedAllowed: false` | never call Jev; reasoning turn on local model only | judgment record with `error: HOSTED_JUDGMENT_NOT_AUTHORIZED`, no request hash sent |
| Jev answer passes but R1 guard fails on the fresh snapshot (agent became busy) | treat as `wait` and log `guard_override` | wake.ack cites the guard |
| Metroplex breaker `jev` open (3 consecutive unavailable) | skip Jev for 15 min, reasoning turn | audit `breaker_open jev` |

Thresholds start at `minConfidence 0.60` for choice and `readyThreshold 0.70` for noul, as `thresholdVersion v1`. The pair spec forbids assuming a universal cutoff (pair spec :70); section 6 makes these versioned and measured.

### 2.4 Staying inside Jev's constraints

- Jev cannot execute tools, grant permissions, or accept tasks (pair spec :34). In this design Jev's output is one field on a `judgment` object. Only Metroplex, holding the `cos` credential, issues commands, and only the task owner can accept (work.mjs:380, 386).
- The `cos` principal needs `work` on the project to `judgment.record` (work.mjs:486). D8 auto-grants `cos` `[work, assign]` at `project.create` so this never needs a per-project step.
- Mock mode is forbidden when `METROPLEX_ENV=live`; a mock record (jev.ts:41-42 marks them "NOT Jev's judgments") must never reach `judgment.record`. Enforce in code: the Python client refuses to persist `mock: true` unless the environment is `pilot`.
- Judgment ids are deterministic: `route:<wakeId>:<cycle>` for routing, `accept:<attemptId>` for Q1, `intake:<cardHash>` for I1. A crash after the HTTP call and before `judgment.record` is recovered by `reconcile` (jev-tool.ts:48-51 pattern): on the next cycle Metroplex checks the snapshot for the id before calling Jev again. If absent, it calls again and accepts that usage may double; it never claims exactly-once billing (pair spec :70).

### 2.5 Other Jev uses in the inventory (same adapter, different question sets)

| Use | Type | State | Question | Threshold |
|---|---|---|---|---|
| I1 classify | choice | message + project list | note / project-N / new_objective | 0.60 |
| I2 intent resolved | noul | message + draft done_when | "Can an observer verify done_when without asking Matthew?" | 0.70 |
| S1 card completeness | score 0-4 | card JSON | rubric: missing fields / vague done_when / no owner / no kill / complete | >= 3 to show |
| Q1 acceptance evidence | score 0-3 | acceptance + result + receipts | rubric: no evidence / claims only / partial receipts / receipts cover acceptance | 3 = nudge owner, 1-2 = owner judgment, 0 = rework |
| Q2 quality checks | noul per check | receipt list + check text | "A receipt shows this check ran and passed" | 0.80 |
| Q4 reserved action | noul | result + reservedActions | "The next step is publish/push/deploy, external contact, or live fleet config" | 0.50 either way triggers a turn |
| L2 lesson litmus | noul | failure/outcome summary | doctrine litmus | 0.70 |

## 3. Concept-to-spec-card pipeline

Conclusion: "any AI tool can add a task" is satisfied by letting any authenticated principal file an inert `proposal` into Teletraan; nothing routes it. "Authority only from Matthew's yes" is satisfied because a proposal becomes a project only through I4 (approval record + `project.create`), and becomes a task only under an already-granted project via a principal that holds that project's `work` grant. The uniform pipeline is enforced by one spec schema validated at `task.create` (Teletraan side, D1) and one card schema validated before the card is shown (Metroplex side, S1/S4).

### 3.1 Lifecycle states

```
                    any AI tool / skill / cron        Matthew on the bot
                              |                              |
                              v                              v
                 +------------------------+     +------------------------+
                 | proposal (inert)       |     | message -> I1 classify |
                 | V1 shape: title, body, |     +-----+-------+----------+
                 | source{type,ref}       |           |       |
                 | contracts.mjs:20-27    |     note  |       | belongs to P
                 +-----------+------------+       |   |       |
                             |               inert   |       v
                             v            proposal   |   +----------------+
                 +------------------------+          |   | I5 sub-concept |
                 | triaged (Metroplex     |<---------+   | -> task.create |
                 | briefing lists it;     |              | under P grant  |
                 | Matthew /card <id>)    |              +----------------+
                 +-----------+------------+
                             | Matthew asks for a card, or I1 = new_objective
                             v
                 +------------------------+
                 | carded: I2 (<=1 q),    |
                 | I3 draft, S1 lint      |
                 +-----------+------------+
                             | bot shows [Yes] [Edit] [Drop]
                             v
                 +------------------------+   Drop / 7-day silence
                 | awaiting_yes           |---------------------> dropped (proposal stays inert)
                 +-----------+------------+
                             | Yes (I4 validity: user id, chat, hash)
                             v
                 +------------------------+
                 | granted: approval.record, project.create (operator-only op,
                 |   work.mjs:184-188, via cos with approval T2), root task.create,
                 |   cos auto-grant [work, assign] (D8)
                 +-----------+------------+
                             | wake created on root task
                             v
                 +------------------------+
                 | decomposed: S2 goals -> S3 tasks with checkpoints + quality checks,
                 |   task.dependencies, each task cites cardGoalId (D9)
                 +-----------+------------+
                             | wakes per task
                             v
                 +------------------------+
                 | running: Path 2 routing; motion checks; Q1-Q4; E1-E4
                 +-----------+------------+
                             | all tasks done (work.mjs:384-392) or Matthew cancels
                             v
                 +------------------------+
                 | done / cancelled: M6 retire temps, L1 outcome, L2 lesson
                 +------------------------+
```

### 3.2 Reconciling the two rules

| Addition | Who may do it | Where it lands | Routed? | Matthew's yes |
|---|---|---|---|---|
| Idea from any AI tool, skill, cron, or `/quick` | any principal with proposal-socket access (V1 today, README :5) | `proposal` object, no projectId | never; Metroplex only lists it in the briefing | needed before it becomes anything |
| Idea from Matthew on the bot | Matthew only (chat + user id check) | card store, then project | after yes | one tap, at project grant |
| Subtask inside a granted project | task owner, project members with `work` grant, `cos` (work.mjs:235) | `task.create` under P | yes, on its `created` wake | not needed; the project yes covers work within `objective` and `scope_in` |
| Sub-concept Matthew sends that belongs to P | Matthew via I5 | `task.create` under P by `cos`, citing a card goal | yes | not needed if I5 noul passes; else one question |
| Scope change inside the objective | owner or `cos` via `task.scope` (work.mjs:254-257) | same task, scopeRevision+1 | yes (wake `scope`) | not needed (R6) |
| Scope beyond `scope_in` or a new objective | nobody automatically | E1 `task.block reason intent`, bot question | no | needed |
| Anything a worker "invents" | workers hold `work` only; `task.create` by a worker is allowed by Teletraan (work.mjs:235 grant check is `work`) | under P | yes | NOT needed today. This is the Paperclip self-minting hole. D9 closes it: `task.create` must cite a `cardGoalId` that exists on P's card, or carry `parentId` of a task the caller owns. |

### 3.3 Spec card schema (project level, shown to Matthew)

```
card:
  card_id:            uuid, hash over the fields below is the approval key (T2)
  title:              <= 80 chars
  objective:          one paragraph, imperative
  done_when:          observable predicate an outsider can check without asking Matthew
  scope_in:           list, what is included
  scope_out:          list, what is excluded (defaults: no publish, no external contact, no fleet config)
  goals:              1..7, each {goal_id, statement, done_when}
  owner:              persistent agent id (must exist, work.mjs:185)
  reserved_actions:   subset of {publish_push_deploy, external_contact, live_fleet_config}, default all three
  hosted_allowed:     bool (maps to project.hostedAllowed, gates Jev and hosted models)
  concurrency_limit:  int, default 2 (project.concurrencyLimit, work.mjs:287)
  provider_call_limit:int, default 200 per project (count, not dollars, work.mjs:199)
  kill:               condition that ends the project (time, count, or event)
  human_only:         bool, default false; when true no task under it is ever routed (section 4 guard)
  source:             {type, ref} of the originating proposal, if any
  spec_template:      version id of the checkpoint/quality-check template used (section 6)
```

### 3.4 Task spec schema (uniform for every task, validated by Teletraan at `task.create`, D1)

Existing required fields: `title`, `objective`, `acceptance` (work.mjs:242 already rejects empty acceptance). Add:

```
task.spec:
  card_goal_id:     required when parentId is null and principal is not operator (D9)
  checkpoints:      1..5 {name, evidence_expected}; the worker's attempt.checkpoint
                    payload names the checkpoint it reached (work.mjs:357 stores it)
  quality_checks:   0..5 {name, how_verified: receipt|test|review}; Q2 asks Jev per check
  done_when:        observable, may equal acceptance; kept separate so acceptance can add evidence rules
  reserved_action:  null | publish_push_deploy | external_contact | live_fleet_config (D6)
  human_only:       bool, inherited from card, default false
  synthetic:        bool, default false; true = test artifact, never routed, auto-cancel at end of cycle
  next_action:      already exists as nextAction (work.mjs:242)
```

Uniformity rule: Metroplex refuses to issue a `task.create` that fails this schema (S4). Teletraan refuses any principal's `task.create` that fails it (D1). Two layers, neither trusts the other.

## 4. Keeping Teletraan in motion

Conclusion: motion is two loops, both deterministic. The wake loop (60 s poll, `wake_pending` T5) reacts to Teletraan's own wakes. The sweep loop (every 5 min) finds what wakes cannot see: stalls, orphans, unassigned ready work, unblockable blocks, and drained projects. The sweep never issues routing commands directly; it raises a durable wake (D10) so every decision goes through the same fenced, idempotent turn path and shows up in `priorDecisions`. The Paperclip "board is live" failure is closed structurally: Metroplex can only act on tasks inside a granted project, proposals have no project, and `human_only` and `synthetic` flags are enforced by Teletraan at `attempt.queue`.

### 4.1 Cadence and caps

| Loop | Cadence | Reads | Cap | Hold conditions (M7) |
|---|---|---|---|---|
| Wake loop | every 60 s | `wake_pending` since cursor (T5) | 1 turn per project at a time (orchestration-wakeups.ts:37 pattern), 3 turns per project per 15-min cycle (P1) | global pause, breaker `turn` open, schedule window (config.py:128-131) |
| Sweep loop | every 5 min | snapshot filtered to `cos`-visible projects (work-service.mjs:88-92) | raises at most 10 wakes per sweep, 1 per task | global pause, breaker `motion` open |
| Briefing | 08:00 and 18:00 local, plus immediate for `reserved`/`authority` escalations (E4) | audit log + snapshot | one message per slot | breaker `briefing` open |
| Jev calls | on demand inside turns | n/a | 20 rps hard (jev.ts:139), 60 per 15-min cycle soft | breaker `jev` open |

Backoff on turn failure: 5 s doubling to 300 s per wake key (orchestration-wakeups.ts:53-57), preserved.

### 4.2 The checks (all read-only until the `wake.raise`)

| Check | Condition (from Teletraan objects) | Threshold v1 | Action |
|---|---|---|---|
| Stalled attempt (M1) | attempt.status `running`, `heartbeatAt` < 5 min old, `progressAt` older than 45 min or null 45 min after start | 45 min | `wake.raise stalled`; R2 favors `assist` (a second contribution with disjoint writableScope, work.mjs:294-298), not replacement (AE3) |
| Dead attempt (M2) | attempt `running`, `heartbeatAt` > 10 min old | 10 min | `wake.raise stalled`; turn may `contribution.create` for a replacement only after the runtime records `attempt.stopped` or `attempt.failed` with `observedStopped` (work.mjs:372-374). Metroplex never fabricates a stop. |
| Unassigned ready (M3) | task `ready`, all `dependencies` done (work.mjs:288), zero contributions, age > 10 min since last wake ack | 10 min | `wake.raise unassigned` |
| Orphaned wake (M4) | wake `acknowledged: false`, no turn in flight, age > 15 min; or ack with empty decision | 15 min | re-queue; if `STALE_WAKE` on ack, drop and log |
| Blocked-on-unrelated | task `blocked` with reason `dep:<taskId>` and that task is `done`; or reason `intent` and a bot reply exists for it | immediate | `task.resume` (work.mjs:262 requires no live attempts) |
| Blocked, siblings idle | task `blocked`, sibling tasks under the same parent are `ready` with no contribution | immediate | raise `unassigned` on each sibling; this is the anti "whole project hung on one item" rule (dependencies are explicit per task, work.mjs:248-253, so a block never cascades unless declared) |
| Progress vs liveness | attempt heartbeat fresh AND `contribution.checkpoint` advanced within the window | n/a | no action, and R6 refuses to reverse the allocation (KTD7: liveness is not progress, silence is not failure) |
| Drained project (M6) | every task terminal, temporary agents `active` | immediate | `agent.retire` per agent (guards at work.mjs:178-180) |
| Allowance (M8) | `providerCalls / providerCallLimit` >= 0.8 | 80 percent | bot note once; at 100 percent Teletraan itself rejects `provider.reserve` (work.mjs:199), the turn records `task.block reason allowance` |
| Cycle cap breach | a project hit 3 turns in the cycle with no state change between turns | 3 | hold project until next cycle, bot note if it repeats 3 cycles (thrash guard) |
| Judgment drift | `judgment` objects for a task with `current: false` outnumber current ones by 5 | 5 | log `spec_churn`, feed to Sky-Lynx as a spec-quality signal (section 6) |

### 4.3 The anti fake-work guard (Paperclip failure closed at both layers)

Paperclip failure recalled from memory: the routing pass consumed every unassigned `todo` within ~20 minutes, so human-only items and a synthetic failure issue became agent work ($1.93 and a fake root cause, feedback_paperclip_board_is_live_fleet_surface.md:11-25). `owner: Matthew` was not honored because every card carried it (:27-31).

Point of intent (Metroplex): the sweep only enumerates tasks whose project has an active `cos` grant, whose card `human_only` is false, and whose task `synthetic` is false. Proposals are not tasks and have no projectId; the sweep cannot see them.

Point of action (Teletraan, D1/D6): `attempt.queue` rejects `HUMAN_ONLY_TASK` and `SYNTHETIC_TASK`. `task.create` rejects a task without `card_goal_id` or an owned `parentId` when the principal is not operator (D9). A synthetic task auto-cancels at cycle end via a Metroplex sweep so it never lingers for triage.

Result: the only way work reaches an agent is (card yes) -> (goal on card) -> (task citing that goal) -> (wake) -> (turn). Every hop is a Teletraan event with a principal.

## 5. Emulate from Paperclip, leave behind

Conclusion: emulate the durable shapes (issue lifecycle, assign-at-creation with a testable done-when, per-issue worktree isolation, bundled findings, unassign-on-close) as Teletraan objects that already exist. Leave behind every mechanism that generated work without intent: timer heartbeats, comment-triggered wakes, self-assignment, a routing pass over an unassigned pool, per-step approvals, dollar budgets, and stand-alone sweep crons.

Grounding: the disposition ledger explicitly carried 30 items, verified 29 as done, and cancelled 13 as Paperclip-only mechanics (build-disposition-ledger.py:24-48); 109 were open at freeze (paperclip-retired-2026-09-09.md:13). The remainder took the default disposition, which I did not read (the ledger's evidence tables sit outside the allowed paths).

| Paperclip mechanism | Verdict | Teletraan/Metroplex equivalent | Evidence |
|---|---|---|---|
| Issue lifecycle (todo, in_progress, blocked, in_review, done, cancelled) | emulate | task statuses `ready, active, blocked, paused, cancelling, cancelled, done` already exist (work.mjs:11-12, 258-273, 384-392); `in_review` becomes "attempt completed, owner acceptance pending" (Q1/Q3) | chassis :370 "in_review is the human half of the loop" |
| Assign at creation, testable done-when, bundle ~6 related findings per issue | emulate | S2 creates tasks with `acceptance`, `done_when`, `checkpoints`, and routes on the `created` wake immediately; decomposition cap 12 per pass, target 4 to 8 leaves per goal | chassis :370 "issue-minting pattern that works" |
| Per-issue git worktree, primary checkout stays clean | emulate | `contribution.writableScope` plus overlap rejection (work.mjs:277-283, 294-298); worker contract requires an isolated linked worktree (external-llm-dispatch.md worker contract item 3) | chassis :240-266 |
| Unassign on close | emulate structurally | `task.complete` requires no live attempts and accepted contributions (work.mjs:387-390); M6 retires temporary agents; no assignee field lingers to re-wake | chassis :117-119 |
| Agent heartbeat as liveness | emulate the signal, not the timer | `attempt.heartbeat` (work.mjs:368-370) is worker-driven liveness; `attempt.checkpoint` (work.mjs:354-357) is progress. Metroplex never wakes an agent on a timer. | chassis :176-178, :357 (Starscream 4h timer cost $30.79 idle) |
| Timer heartbeats / routines that wake agents on a schedule | leave behind | none. Wakes come only from task events (work.mjs:152-157) and sweep-raised wakes with a reason (D10). | chassis :357 |
| Comment-triggered wakes (decline comment re-wakes self) | leave behind | there are no comments. Escalation is `task.block` with a reason enum; Matthew's reply is applied by E3 as one command. | chassis :360-367 (MAI-280, three runs on one issue) |
| Agents self-assigning unassigned backlog | leave behind | workers hold `work` only; `task.assign` needs `assign` (work.mjs:245); D9 blocks self-minted tasks | chassis :218-222 (MAI-7 duplicate PRs) |
| Daily routing pass over the unassigned pool (6-issue cap) | leave behind, replace with event routing | R1/R2 per wake; M3 sweep raises `unassigned` only inside granted projects | board-is-live memory :12-14 |
| Board approval for hires | leave behind | `agent.recruit` under the project `assign` grant (work.mjs:168-173); no approval object. The Paperclip approval was bookkeeping anyway. | chassis :143-145 |
| Per-step HIL approval, HIL push lock | leave behind the per-step gate, keep the boundary | reserved actions as data (T3/D6), `teletraan-publish` outside the worker sandbox (teletraan-core/bin/teletraan-publish exists) | chassis :190-217 (lock proven porous) |
| Dollar budgets, spend-cap hook | leave behind entirely | count limits only: `concurrencyLimit`, `providerCallLimit` (work.mjs:186, 199), turns per cycle, Jev calls per cycle. `BudgetEnforcer` (safety.py:149-255) is deleted. `usage.cost` recording: strike (P3 recommend). | chassis :134-137, :184-186 (budgets never fired) |
| Unassign sweep, agent-error sweep, recovery-action sweep, liveness sentinel as separate crons | leave behind as crons, fold the checks into the 5-min sweep (section 4.2) | one owner (Metroplex), one audit log, one pause switch; displace-dont-add rule | crontab-pre-removal.txt :222-241 |
| QC inspector weekly cron | fold into Q1/Q2 (Jev score per result) plus the Sky-Lynx window analysis | section 6 | crontab-pre-removal.txt :244 |
| Portable export / disposition ledger | emulate as a capability, later | `history` action exists per task (work-service.mjs:95-99); an export CLI is out of scope for this plan | build-disposition-ledger.py |
| Adapter runtime quirks (claude_local flags, MCP inheritance, permission bypass) | leave behind | T3/native runtime contract per pair spec W2; not Metroplex's concern | chassis :192-208 |

## 6. Matrix and Sky-Lynx connectivity: the recursive continuous learning loop

Conclusion: RCL is one loop with four measured hops, each with a single writer. Outcomes leave Teletraan through Metroplex (hop 1). Sky-Lynx reads outcomes and proposes changes to routing weights, Jev question/threshold versions, and spec templates (hop 2). Matthew accepts or rejects; Metroplex adopts accepted proposals as versioned config (hop 3). Every later outcome carries the config versions that produced it, so Sky-Lynx can measure whether the change helped and propose the next one, or a rollback (hop 4). Matrix is the long-term memory that both hops 1 and 2 consult so the loop does not re-learn what it already knows. Nothing in the loop writes to Teletraan except Metroplex, and nothing writes Metroplex config except an accepted proposal.

### 6.1 Current state, as read

- Sky-Lynx's Metroplex reader queries `build_jobs`, `triage_decisions`, `priority_queue`, `publish_jobs`, `gate_status`, `cycles` (metroplex_reader.py:45-124). All six tables belong to the gate pipeline the draft spec retires. After the strip this reader returns nothing.
- Sky-Lynx already consumes a file event stream at `~/.local/share/skylynx-events/` written by Metroplex's `EventEmitter` (event_emitter.py:15, 34-70; analyzer.py:225-240 aggregates it on every run; trigger_listener.py:16-20 fires a reactive run on 3 consecutive failures or a success rate below 0.40 over 20 events, with a 12 h cooldown).
- Sky-Lynx proposes config changes through `ProposalTracker` (proposal_tracker.py:86-186): rows `parameter, current_value, proposed_value, rationale, status in (proposed, accepted, rejected, escalated)`, Telegram notify on propose, squawk every 24 h. Matthew accepts with `apply-proposal <id>`.
- Sky-Lynx's `claudeclaw_writer.py` writes recommendation files for a ClaudeClaw daily loop (claudeclaw_writer.py:1-8); `mission_reader.py` reads `~/projects/claudeclaw/store/claudeclaw.db` (mission_reader.py:7-19), a path that no longer exists (spec "Verified current state"). Both are dead ends today.
- Matrix: Sky Lynx is the sole consumer, peripheral producers push to one central drop queue (matrix CLAUDE.md:7, README "Hub-and-spoke law"); ingestion is a file queue with owner/sink/kill (CLAUDE.md:24); `fleet_memory` is for durable learnings, `hive_mind` for events (fleet-memory-doctrine.md:13-23); access is the matrix-memory MCP on loopback only (:36-38).
- Sky-Lynx cadence: Wed and Sun 02:00, auto-apply cap 5 CLAUDE.md rules per week, event-driven triggers were the planned L5 step (sky-lynx-cadence-upgrade.md:7-19).

### 6.2 The loop

```
      +------------------------------------------------------------------+
      |                          TELETRAAN (truth)                       |
      |  task, attempt, judgment, wake.priorDecisions, project limits    |
      +------------------------------+-----------------------------------+
                                     | wake done / stopped / cancelled
                                     v
   hop 1  METROPLEX  L1: outcome event -> skylynx-events/<ns>.json   (single writer, source_repo=metroplex)
                     L2: lesson (if litmus passes) -> Matrix drop queue (single fleet_memory writer for task outcomes)
                     every event carries: questionVersion, thresholdVersion, routingWeightsVersion,
                       specTemplateVersion, judgmentIds, decision chain, outcome, cause
                                     |
                                     v
   hop 2  SKY-LYNX   teletraan_reader.py (D20) reads snapshot read-only via a `skylynx` principal
                     with no grants (sees nothing) ... so instead reads the outcome events + a
                     Metroplex-exported daily digest JSON (D21). Aggregates per version:
                       routing: assign->done rate per (agent, capability), reassign rate, stall rate
                       Jev: agreement between Jev choice and reasoning fallback; confidence vs outcome
                       spec: churn (scopeRevision bumps per task), rework rate, checks that never fire
                     memory_search Matrix for prior lessons before proposing (doctrine :27-29)
                     -> ProposalTracker.propose(parameter, current, proposed, rationale)
                                     |
                                     v
   hop 3  MATTHEW    accept / reject on Telegram (squawk after 24 h, proposal_tracker.py:187-239)
                                     |
                                     v
          METROPLEX  L4: polls proposals.db read-only; on `accepted` for an allowlisted parameter,
                     bumps the matching version string and reloads. Never touches Teletraan.
                                     |
                                     v
   hop 4  (next outcomes carry the new versions) -> Sky-Lynx compares windows before/after the
          version bump -> proposes keep, tune, or rollback. The rollback is itself a proposal.
```

Recursion property: a proposal is evaluated by the same pipeline that it changed, on outcomes tagged with its version. Sky-Lynx's own proposal quality becomes a measured series (accept rate, time-to-rollback), and that series is a Sky-Lynx input on the next run. No hop can shortcut another: Sky-Lynx cannot change Metroplex config, Metroplex cannot change Sky-Lynx's analysis, and neither can write Teletraan work state.

### 6.3 Single writer per direction

| Direction | Writer | Reader | Medium | Who else may write |
|---|---|---|---|---|
| Teletraan -> outcomes | Metroplex (`cos` reads snapshot, writes events) | Sky-Lynx | `~/.local/share/skylynx-events/*.json` with `source_repo=metroplex` (event_emitter.py:57-62) | other repos with their own `source_repo`; never Sky-Lynx |
| Metroplex -> Matrix | Metroplex L2 | Sky Lynx as Matrix's sole consumer (matrix CLAUDE.md:7) | Matrix drop queue file (CLAUDE.md:24). Not the MCP: Metroplex is a Python daemon and the MCP is stdio loopback for sessions (doctrine :36-38) | nobody for task outcomes; agents may still write their own fleet_memory learnings via MCP per doctrine, but not task events (P2) |
| Matrix -> Metroplex | Matrix (read) | Metroplex R2 state builder (<= 3 hits) | read-only search; path to be chosen (open question 3): a small Node CLI wrapping the same search, or an HTTP loopback endpoint | none |
| Sky-Lynx -> Metroplex config | Sky-Lynx via ProposalTracker; Matthew's accept is the write | Metroplex L4 | `proposals.db` (proposal_tracker.py:32-34 path) read-only from Metroplex | nobody writes `accepted` except the CLI Matthew runs |
| Sky-Lynx -> Teletraan | none | n/a | n/a | Sky-Lynx never holds a Teletraan credential |
| Sky-Lynx -> ~/CLAUDE.md | unchanged (auto_applicator.py:22-24) | n/a | out of this loop | n/a |

### 6.4 What each hop measures (v1 metric set)

| Hop | Metric | Source fields | Feeds |
|---|---|---|---|
| Routing quality | done rate per (agent, capability, questionVersion); reassign count per task; stall wakes per task | L1 events: decision chain, judgmentIds, outcome | routing weights proposal (`METROPLEX_ROUTING_WEIGHTS_VERSION`) |
| Jev calibration | P(outcome=done given confidence bucket); reasoning-turn disagreement rate; unavailable rate | judgment objects mirrored in L1 events | `thresholdVersion` proposal (raise/lower minConfidence per question id) |
| Spec quality | scopeRevision bumps per task; rework contributions per task; quality checks that never produce a receipt; time from `created` wake to first `progressAt` | L1 events + judgment drift signal (section 4.2) | `specTemplateVersion` proposal (change default checkpoints/quality checks per goal type) |
| Escalation load | escalations per project per day by reason enum; median Matthew response time | L1 events + audit log | proposal to widen or narrow `scope_out` defaults; a rise in `intent` escalations means cards are under-specified (I2 question policy) |
| Loop health | proposal accept rate; time-to-rollback; proposals with no measurable delta after 2 windows | proposals.db + events | Sky-Lynx's own prompt (self-measure, the recursive part) |

### 6.5 Guard rails on the loop

- No auto-apply into Metroplex. Sky-Lynx's `MAX_AUTO_CHANGES_PER_WEEK` applies to `~/CLAUDE.md` only; Metroplex parameters are always Matthew-accepted. This matches "HIL gates must not be auto-approved".
- Allowlisted parameters only: `questionVersion`, `thresholdVersion`, `routingWeightsVersion`, `specTemplateVersion`, `stallThresholdSeconds`, `unassignedThresholdSeconds`. A proposal naming anything else is logged and ignored by L4.
- One change per parameter per window, so hop 4 can attribute deltas.
- Metroplex tags every outcome event with all four version strings; Sky-Lynx refuses to compare windows with mixed versions inside one bucket.
- Matrix lessons carry `agent=metroplex, topic=<project slug>`, facts only (doctrine :30-31). Secrets never (doctrine :32).

## 7. Required changes beyond the draft spec (numbered deltas)

Conclusion: 11 Teletraan deltas, 8 Metroplex deltas, 3 Sky-Lynx deltas. T1 to T5 and the draft's Metroplex list stay as written; these are additions. Each delta names the file it touches and the guard it adds.

### Teletraan (pilot branch `feat/organization-continuity-pair`, `src/domain/work.mjs` unless noted)

| # | Delta | Where | Why |
|---|---|---|---|
| D1 | Task spec fields validated at `task.create`: `spec.checkpoints` (1..5), `spec.qualityChecks` (0..5), `spec.doneWhen`, `spec.reservedAction`, `spec.humanOnly`, `spec.synthetic`. Reject `INVALID_TASK_SPEC`. | work.mjs:234-243 | uniform pipeline; second layer behind Metroplex S4 |
| D2 | `proposal` object kind and `proposal.file` operation open to any authenticated principal, mirroring V1 `normalizeProposal` (contracts.mjs:20-27). No projectId, never wakes. `snapshot` includes proposals only for `operator` and `cos`. | work.mjs decide(), work-service.mjs:85-92 | "any AI tool can add a task" lands here, inert; one store, displace-dont-add |
| D3 | `wake_pending` action (T5) returns unacknowledged wakes with `seq > cursor` and their task/project revisions in one response. | work-service.mjs | avoid a full snapshot per poll |
| D4 | `task.assign` payload accepts optional `judgmentId`; Teletraan verifies it exists, belongs to the task, and is `current`, else `JUDGMENT_NOT_CURRENT`. | work.mjs:244-247 | routing evidence is linked, not just cited in prose |
| D5 | `attempt.queue` rejects `HUMAN_ONLY_TASK` and `SYNTHETIC_TASK` from any non-operator principal. | work.mjs:284-315 | anti fake-work at the point of action |
| D6 | Reserved actions as data: project `reservedActions[]` (T3) plus task `spec.reservedAction`; `attempt.queue` on a task whose `reservedAction` is in the project's list rejects `RESERVED_ACTION_REQUIRES_APPROVAL` unless an `approval.record` cites that task. | work.mjs:284-315, approval kind from T2 | closes the "worker performs a reserved step" gap without per-step gates for everything else |
| D7 | `project.pause` / `project.resume` operations (operator or `cos`): pause marks the project and makes `attempt.queue` reject `PROJECT_PAUSED`; running attempts continue; resume raises one `unassigned` wake per ready task. | work.mjs decide() | the kill switch is per project, not only per task; C5 drains without duplicates |
| D8 | `project.create` auto-grants `cos` `[work, assign]` alongside the owner grant. Not `review`. | work.mjs:184-188 | Metroplex can route and record judgments; cannot accept |
| D9 | `task.create` by a non-operator principal must carry `spec.cardGoalId` that exists on the project's stored card goals, or a `parentId` whose owner is the principal. Reject `TASK_WITHOUT_GOAL`. Store card goals on the project at `project.create` (`goals[]`). | work.mjs:234-243, 184-188 | closes Paperclip self-minting (MAI-7 class) |
| D10 | `wake.raise` operation for `cos` with reason in `{stalled, unassigned, orphan, unblock}`; same key scheme `${projectId}:${taskId}:${reason}` and cycle increment (work.mjs:152-157), so sweep-raised wakes are fenced like native ones. | work.mjs decide() | sweeps become durable, idempotent, and visible in `priorDecisions` |
| D11 | `judgment.record` keeps `recordedBy` (work.mjs:487) and additionally requires `questionVersion`, `thresholdVersion`, `inputHash`, `status`, `fallback` fields present (schema from jev-adapter.ts:108-143). Reject `INVALID_JUDGMENT`. | work.mjs:485-487 | Sky-Lynx calibration needs these on every record |

Not changed: `contribution.accept` and `task.complete` stay owner-only (work.mjs:380, 386). `project.create` stays operator-only in the domain; the `cos` path goes through T2's approval record at the service layer.

### Metroplex (Python, `~/projects/st-metro/metroplex/`)

| # | Delta | File | Notes |
|---|---|---|---|
| D12 | `jev_client.py`: port of jev-adapter.ts validation (question schema, response schema, label/score checks, low-confidence and unavailable records); 30 s timeout; no retries; refuses to persist mock records outside `pilot`. | new | the Python twin of jev-adapter.ts:6-79, 96-162 |
| D13 | `motion.py`: the 5-min sweep from section 4.2, emitting `wake.raise` only; thresholds from config with versions. | new | replaces health.py checks that read retired tables (health.py:132-153 stuck builds, :195 queue drain, :227 budget) |
| D14 | `spec.py`: card schema and task spec schema (section 3.3, 3.4) as JSON Schema; S1/S4 lint; templates keyed by `specTemplateVersion`. | new | one schema, both layers |
| D15 | `turn_runner.py` gains a `decompose` mode (S2) bounded to 12 `task.create` per pass and a `route` mode that consumes a Jev judgment first (section 2.3). | draft spec file | the reasoning turns are two prompts, not one |
| D16 | `matrix_writer.py`: writes one drop-queue item per terminal task to Matrix's queue directory, owner/sink/kill header per Matrix CLAUDE.md:24; litmus (L2) before write. | new | P2 accepted in this plan as the single fleet_memory writer for task outcomes |
| D17 | `event_emitter.py` kept; add event types `task_done`, `task_failed`, `task_cancelled`, `routing_decision`, `escalation`, `judgment` with the four version tags and the decision chain. | event_emitter.py:34-70 | Sky-Lynx hop 1 |
| D18 | `learning_config.py`: polls `proposals.db` read-only for `accepted` rows on the allowlist (section 6.5) and bumps versions; audit entry per adoption. | new | hop 3 |
| D19 | `safety.py`: `CircuitBreaker` gate literal becomes `intake, turn, motion, briefing, jev` (safety.py:36, 52, 74, 90 use `Literal["triage","build","publish"]`); delete `BudgetEnforcer` (:149-255); `CycleCaps` gains `max_turns_per_project`, `max_wakes_per_sweep`, `max_jev_per_cycle` (:124-146 has only approve). | safety.py | runaway guards without dollars |

### Sky-Lynx (`~/projects/st-metro/sky-lynx/src/sky_lynx/`)

| # | Delta | File | Notes |
|---|---|---|---|
| D20 | `teletraan_reader.py` replaces `metroplex_reader.py`: reads the Metroplex daily digest JSON (D21) and the outcome events; never opens a Teletraan socket. | new; retire metroplex_reader.py:22-124 | Sky-Lynx holds no Teletraan credential |
| D21 | Metroplex writes `data/digest/<date>.json` (per-version aggregates from section 6.4) for Sky-Lynx; Sky-Lynx's "pipeline" scope (analyzer.py:203-205) reads it. | metroplex + analyzer.py | keeps the hub-and-spoke rule: one producer file, one consumer |
| D22 | Retire `claudeclaw_writer.py` (writes to a loop that no longer runs) and repoint `mission_reader.py` (dead DB path, mission_reader.py:19) or retire it. Proposals for Metroplex go through `ProposalTracker` only. | claudeclaw_writer.py, mission_reader.py | displace-dont-add |

## 8. Acceptance examples (observable, testable)

Conclusion: 18 examples across five groups. Each names the Teletraan objects that must exist afterward, so a test reads the snapshot or history, never a log line.

Jev routing (section 2)
- J1. Given a `created` wake on a task with two eligible members, Metroplex records exactly one `judgment` with id `route:<wakeId>:<cycle>`, `status: judged`, `questionVersion` and `thresholdVersion` set, and one `task.assign` whose payload `judgmentId` equals that id. History for the task shows `judgment.record` before `task.assign`.
- J2. Given Jev returns `choice` with `confidence 0.41` under `minConfidence 0.60`, the judgment is `status: needs_review, fallback: reasoning_review`, a reasoning turn runs, and `wake.ack.decision` names both the judgment id and the turn's decision.
- J3. Given the TypeSafe endpoint returns HTTP 503, the judgment is `status: unavailable`, no second HTTP call occurs in the same cycle (assert via a counting fetch stub), and routing still completes by reasoning turn.
- J4. Given `task.scope` bumps `scopeRevision` while the Jev call is in flight, the judgment persists with `current: false` and no `task.assign` cites it; the next wake cycle records a new judgment.
- J5. Given `project.hostedAllowed: false`, no request reaches the fetch stub, and the judgment record carries `error: HOSTED_JUDGMENT_NOT_AUTHORIZED`.
- J6. Given a `mock: true` judgment in `METROPLEX_ENV=live`, `jev_client` raises before `judgment.record`; the snapshot has no judgment for that wake.
- J7. Given Jev picks agent A confidently but a fresh snapshot shows A at its live-attempt cap, no `task.assign` occurs, `wake.ack.decision` starts with `wait: guard_override`, and the judgment is still stored.

Concept-to-spec (section 3)
- P1. Given any principal files `proposal.file`, the snapshot for `cos` lists it, no task or project references it, and 24 h of sweeps raise no wake for it.
- P2. Given Matthew taps Yes on a valid card, the receipts show `approval.record`, `project.create` (with `goals[]`), root `task.create`, grants for owner `[work, assign, review]` and `cos` `[work, assign]`, and within one turn between 4 and 12 child `task.create` events each carrying `spec.cardGoalId` in the card's goal set.
- P3. Given a worker principal issues `task.create` without `spec.cardGoalId` or an owned `parentId`, Teletraan rejects `TASK_WITHOUT_GOAL`; no wake is created.
- P4. Given `task.create` with empty `spec.checkpoints`, Teletraan rejects `INVALID_TASK_SPEC`; Metroplex's S4 lint rejects the same payload before sending (assert both, independently).
- P5. Given a card with `human_only: true`, its tasks exist, sweeps raise no wakes for them, and a forced `attempt.queue` by `cos` is rejected `HUMAN_ONLY_TASK`.

Motion (section 4)
- K1. Given a running attempt with `heartbeatAt` refreshed every minute and `progressAt` unchanged for 46 minutes, the sweep issues `wake.raise stalled`, and the following turn creates a second contribution with disjoint `writableScope` rather than a replacement (assert one `contribution.create`, zero `attempt.stopped` from `cos`).
- K2. Given task B `blocked` with reason `dep:A` and A transitions to `done`, the next sweep issues `task.resume` on B and B's `created`-style wake routes it. Sibling task C, never blocked, shows attempts progressing throughout (AE5 shape).
- K3. Given a `synthetic: true` task created for a smoke test, it is `cancelled` by the end of the cycle without any `attempt.queue` ever appearing in its history.
- K4. Given `/pause` on the bot, no `attempt.queue` appears for 15 minutes while an already running attempt keeps heartbeating; `/resume` raises exactly one `unassigned` wake per ready task and no duplicate `contribution.create` (C5 re-proven).
- K5. Given Metroplex is SIGKILLed between `task.assign` and `wake.ack`, restart re-enters the same wake cycle, `task.assign` replays idempotently (same `commandId`, same receipt, work.mjs:97-101), and exactly one attempt exists (AE6 re-proven).

Learning loop (section 6)
- F1. Given a task reaches `done`, exactly one event file with `source_repo=metroplex`, `event_type=task_done`, and all four version strings appears, and at most one Matrix drop-queue item is written (zero when the L2 litmus fails).
- F2. Given Sky-Lynx proposes `thresholdVersion v1 -> v2` and Matthew runs `apply-proposal`, Metroplex's next judgment record carries `thresholdVersion: v2`, and the audit log has one `config_adopted` entry. A proposal for a non-allowlisted parameter produces an `config_ignored` entry and no change.
- F3. Given two analysis windows tagged `v1` and `v2`, Sky-Lynx's report contains a per-version routing table, and a window with mixed versions is reported as "not comparable" rather than averaged.

## 9. Risks and failure table

Conclusion: the three risks that matter are (1) Jev confidence looking like authority, (2) sweeps raising wakes faster than turns drain them, and (3) the learning loop optimizing for what is measured (done rate) over what Matthew wants. Each has a detection signal in Teletraan or the audit log and a recovery that does not need Matthew.

| Failure | Detection | Recovery |
|---|---|---|
| Jev answers become de facto authority (turns rubber-stamp) | disagreement rate between Jev choice and reasoning fallback drops to 0 over 50 judgments while done rate falls (section 6.4) | Sky-Lynx proposes raising `minConfidence`; Metroplex `jev` breaker can be opened by hand (`/jev off`) so all routing goes through turns |
| Wake storm: sweep raises `stalled` on many tasks at once, turns cannot drain | `wake_pending` count grows across 3 polls; cycle cap hits on > 3 projects | sweep cap (10 per sweep) and per-project turn cap hold it; if `wake_pending` > 50, Metroplex auto-pauses sweeps (not turns) and messages once |
| Assist loop: `stalled` wake -> assist contribution -> both stall -> another assist | > 2 live contributions on one task | R2 guard: never more than 2 contributions per task; third stall escalates `intent` |
| Decomposition explosion (S2 produces too many or recursive tasks) | > 12 `task.create` in one turn, or depth > 3 | hard cap in turn_runner; Teletraan `CHILD_WORK_NOT_DONE` (work.mjs:391) already prevents parent completion until children finish, so orphans are visible |
| Self-minting through `parentId` (worker creates a child under a task it owns, forever) | children per task > 8, or child created by a temporary agent | D9 requires the caller to OWN the parent; temporary agents own nothing they did not get by `task.assign`; sweep flags `child_fanout` |
| Reserved action performed anyway (worker publishes) | `teletraan-publish` refuses without an approval event; a push outside it is caught by branch protection, not Metroplex | capability absence stays the real boundary (external-llm-dispatch.md worker contract item 4); Metroplex D6 is a second check, not the first |
| Matthew's Yes spoofed | I4 checks user id, chat id, card hash; T2 stores them | attested by the bot process only (spec T2 honest limit); a rejected Yes is logged and the card stays `awaiting_yes` |
| Metroplex crash mid-turn | wake stays unacknowledged; orphan sweep (M4) after 15 min | idempotent replay by `commandId` (work.mjs:97-101); `STALE_WAKE` if the cycle moved (work.mjs:196) |
| TypeSafe outage | `jev` breaker opens after 3 `unavailable` | reasoning turns continue; breaker retries every 15 min; no queued Jev backlog is ever replayed |
| Matrix drop queue unavailable | write fails, logged `matrix_write_failed` | outcome event (hop 1) still written; lesson retried once next sweep, then dropped (Matrix is memory, not truth) |
| Sky-Lynx proposes a bad threshold and Matthew accepts | hop 4 window comparison shows done rate down or escalations up | Sky-Lynx proposes rollback; Matthew can `reject`/`apply` the rollback; versions make the revert exact |
| Measured proxy drifts from intent (high done rate, low quality) | Q1 score distribution shifts up while owner rework contributions rise | owner acceptance stays human-agent, not Jev; Sky-Lynx tracks rework rate alongside done rate (section 6.4) |
| Breaker opens on a transient and stays open silently | `health.py` style check reports any breaker open > 1 h | briefing includes breaker state; `metroplex reset --gate <name>` exists (CLAUDE.md:42-43) |
| Proposals.db and Metroplex disagree on "current value" | L4 compares `current_value` to its live version before adopting | mismatch logs `config_stale_proposal` and does not adopt |
| Provider allowance exhausted mid-project | `PROJECT_ALLOWANCE_EXHAUSTED` on `provider.reserve` (work.mjs:199) | turn records `task.block reason allowance`; M8 warned at 80 percent; Matthew raises the count |

## 10. Open questions for Matthew (each with a recommendation)

1. Where do inert concepts live: the V1 proposal socket (teletraan-core, live) or a `proposal` kind in the pilot store (D2)? Recommendation: D2 in the pilot store, mirroring V1's `normalizeProposal` shape, so the pilot has one store; migrate V1's 10 unadmitted proposals as `proposal.file` events when the backlog moves (decision 7).
2. Who decomposes: Metroplex at grant time (S2), or the owner agent on its first turn? Recommendation: Metroplex drafts the first decomposition (so every project is uniform) and the owner may `task.scope` or add children under D9. Owner-led decomposition can be a later `specTemplateVersion`.
3. Matrix read path for Metroplex (Python daemon, MCP is stdio loopback for sessions): a Node CLI wrapper around the same search, or a loopback HTTP route on Matrix? Recommendation: CLI wrapper first (no new port, no token surface), capped at 3 hits per routing state.
4. Jev thresholds v1: `minConfidence 0.60` (choice), `readyThreshold 0.70` (noul), Q2 per-check `0.80`. Recommendation: accept as v1 and let hop 4 tune them; do not hand-tune before 50 judgments exist.
5. Candidate cap for the routing choice: 6 plus 3 escape labels. Recommendation: accept; larger sets dilute the distribution and approach the 32k state limit with Matrix hits included.
6. Stall thresholds v1: progress 45 min, heartbeat 10 min, unassigned 10 min, orphan 15 min. Recommendation: accept, versioned as `stallThresholdSeconds` so Sky-Lynx can propose changes.
7. `cos` grant shape: `[work, assign]` and never `review` (D8). Recommendation: accept; Metroplex must not be able to accept work, that keeps "thin, replaceable judge" true.
8. Reserved-action enforcement D6: reject `attempt.queue` on a reserved task without an approval, versus letting the attempt run and blocking only at the boundary. Recommendation: D6 as written; a worker should never start a task whose whole point is a reserved step, and the capability boundary remains the real wall.
9. Card expiry 7 days (spec open question 3). Recommendation: accept; expired cards revert to inert proposals and appear in the briefing once.
10. Sky-Lynx cadence for the Teletraan digest: keep Wed/Sun 02:00 plus the existing reactive trigger (3 consecutive `task_failed` or done rate < 0.40 over 20, trigger_listener.py:18-20)? Recommendation: yes, reuse as-is; the event stream is already the L5 "event-driven" step the cadence memory planned.
11. `usage.cost` telemetry (P3): strike or keep passive? Recommendation: strike, per decision 6; `providerCallLimit` counts are the only allowance and Sky-Lynx never sees dollars.
12. Model for Metroplex reasoning turns (M1): Claude CLI subprocess with the OAuth env token, DeepInfra, or local Qwen. Recommendation: adapter first; default DeepInfra for `decompose`/`route` turns because it is already wired (spec "Verified current state"), local Qwen for `hostedAllowed: false` projects, Claude CLI only once the OAuth token proposal lands.
13. Human-only marker name: `human_only` on card and task (section 3.3, 3.4). Recommendation: accept; it is the field the Devpost AAR asked for (`route: human-only`, board-is-live memory :35-37), now enforced at `attempt.queue` instead of by convention.
14. V1 backlog grouping (decision 7): Metroplex proposes it after live. Recommendation: the proposal is a set of cards (one per project group) sent through the normal I3/I4 path, so the 50 tasks enter only under a Matthew yes, and each imported task gets `spec.cardGoalId` mapped at import (`task.import` path, work.mjs:219-226, needs D1 fields defaulted for legacy rows).

AGENT COMPLETE: Metroplex decision layer planned: 24-decision inventory, Jev as evidence-only routing judge with versioned thresholds and recorded fallbacks, proposal-to-card-to-grant pipeline with Matthew's yes at one point, deterministic motion sweeps raising fenced wakes, Paperclip emulate/leave table, and a four-hop recursive continuous learning loop over Matrix and Sky-Lynx with single writers; 22 numbered deltas, 18 acceptance examples, 15-row failure table, 14 open questions.
