# Path B activation plan: Teletraan work service + Metroplex (+ worker execution)

Date: 2026-09-25. Status: PLAN, nothing executed. Decision record:
`~/vault/decisions/2026-09-24-metroplex-cos-router.md`. Authority rule this plan flips:
`~/.claude/rules/external-llm-dispatch.md` ("Path status" line).

## Outcome and done-state

Matthew sends an idea to `@m2ai_metroplex_bot`, taps Yes once, and the project's tasks are
routed to real agents that actually execute them in isolated worktrees, with owners accepting
results and reserved actions coming back to Matthew. Done when one real, low-stakes project
completes end to end under Path B and the watched phase shows no guard failures.

## Verified current state (read 2026-09-25)

- Live V1 `teletraan.service`: `ExecStart=/usr/bin/node ~/projects/teletraan-core/src/api/server.mjs`,
  state `~/.local/state/teletraan`, T3 at `127.0.0.1:3773`, `Restart=on-failure`. Checkout is on
  branch `codex/paperclip-exit` at `784a11a`, clean. No git remote.
- `teletraan-core` `main` = `51f4799`: the deployed line plus the pilot and CoS deltas. Full
  suite 99/99 on `main` (includes the V1 tests). Relative to the deployed branch, the only V1
  runtime files it changes are the T3 adapter (`src/adapters/t3/client.mjs`,
  `ws-rpc-runner.mjs`): the adapter's allowed T3 commands widen by `thread.create` and
  `thread.meta.update` (used by managed T3 bindings).
- The work service is a separate process (`src/cli/work-server.mjs STATE_DIR CREDENTIALS_JSON`,
  env `TELETRAAN_WORK_APPROVER_IDS`, optional `TELETRAAN_WORK_T3_CONFIG`) with its own socket and
  `work.sqlite`. No systemd unit exists for it. `systemd/` has only the V1 template.
- Metroplex `main` = `c3a0fb3`; units `metroplex` and `metroplex-watchdog` disabled; the installed
  `~/.config/systemd/user/metroplex.service` still runs the retired `run-all`. The repo unit
  `deploy/metroplex.service` runs the CoS daemon but only sets `METROPLEX_ENV=pilot`; the socket,
  token and approver env must be added.
- Approver id: in the live E2E the approval's `fromId` equalled `METROPLEX_TELEGRAM_CHAT_ID`
  (a private chat), so that value is Matthew's Telegram user id.
- Two proposal stores exist: V1's `proposal.sock` (used by `/quick` and other tools) and the work
  service's `proposal.file` (the one Metroplex reads). V1 holds 50 tasks and 12 proposals.
- Execution: nothing runs `api` attempts yet. The CCOS pilot's `scripts/run-managed-work-host.ts`
  (branch `ccos-continuity-pair`, uncommitted beyond `8de84f13`) runs native attempts with
  OpenAI-compatible model profiles and an optional `orchestrator` section. That section is the
  pilot orchestrator Metroplex displaced: it must stay unset. T3 managed bindings also need the
  `t3-continuity-pair` branch (`7d45d5163`).

## Decisions needed before Phase 1

1. **Which real agents join the organization** as persistent identities (owners and workers), and
   their capabilities. The E2E used fixtures. Recommendation: start with two, one owner and one
   worker, both backed by the native host on local Qwen.
2. **Where `/quick` and other tools file proposals once B is live.** Recommendation: the work
   service (`proposal.file`), via a dedicated low-privilege `intake` credential; V1 keeps its
   store for Path A until the backlog moves (Phase 4). Until then Metroplex cannot see `/quick`
   captures.
3. **Switch the V1 checkout to `main`, or run the work service from a separate `main` checkout.**
   Recommendation: switch (one codebase, V1 tests pass on `main`); the T3 allowlist widening is
   the only V1 behavior change.
4. **Merge the CCOS and T3 pilot branches** for execution (Phase 3). They are Astra-approved and
   unchanged since, but unreviewed against the CoS deltas; recommend one independent review pass.

## Phases

### Phase 1: Teletraan work service live (reversible, no authority change)

1. Snapshot: `crontab -l` backup is not needed; copy `~/.local/state/teletraan` (V1) and note the
   V1 commit (`784a11a`).
2. `git -C ~/projects/teletraan-core checkout main`; `systemctl --user restart teletraan.service`;
   read-only checks: V1 `teletraan list` works, proposal socket answers, `npm test` 99/99.
3. Create `~/.local/state/teletraan-work` (700) and a credentials file (600) with `operator`,
   `cos`, `runtime` (and `intake` if decision 2 is taken); write `cos.token` to
   `~/.config/metroplex/cos.token` (600).
4. Add `systemd/teletraan-work.service.in` to teletraan-core (versioned), install it with
   `TELETRAAN_WORK_APPROVER_IDS` set to Matthew's id, `Restart=on-failure`. Owner: Matthew. Sink:
   journal + `work.sqlite`. Kill: `systemctl --user stop teletraan-work`.
5. Create the agents from decision 1 with the operator credential.
Exit check: work service answers `snapshot` as `cos`; V1 unaffected. Path B still NOT ACTIVE.

### Phase 2: Metroplex live, routing only (watched)

1. Update `deploy/metroplex.service`: add `METROPLEX_WORK_SOCKET`, `METROPLEX_COS_TOKEN_FILE`,
   `METROPLEX_APPROVER_IDS`; `After=teletraan-work.service`. Install over the stale unit;
   `metroplex-watchdog.timer` stays disabled (retired).
2. Flip the status line in `external-llm-dispatch.md` to "B is ACTIVE", in the same change as
   step 3; update the mirror.
3. `systemctl --user enable --now metroplex`. First start skips the Telegram backlog and says so.
4. Watched phase (trust ladder rung: watched): `METROPLEX_ENV=pilot`, one low-stakes idea,
   Matthew reads every card and `data/decisions.log` daily. Without Phase 3, attempts queue and
   the M6 sweep reports "attempts never started" after 15 minutes: expected until execution lands.
Exit check: a card Yes creates a project, tasks route, escalations reach Telegram, `/pause`
and `/resume` work against the real service.

### Phase 3: worker execution

1. Review, then merge `ccos-continuity-pair` (and `t3-continuity-pair` if T3 bindings are wanted).
2. Native host config: `runtime` connection to the work socket, profile(s) on local Qwen
   (`http://10.0.0.42:8080/v1`, `hosted: false`), `workspaces` mapping, NO `orchestrator`
   section. Run as its own unit. Owner: Matthew. Kill: stop the unit; running attempts are
   reconciled by Teletraan's stop proofs.
3. One real, low-stakes project end to end; owner acceptance; one reserved action approved
   through the bot and published through `teletraan-publish`.

### Phase 4: consolidate (displace, don't add)

1. Repoint `/quick` (and other proposal writers) to the work service (decision 2).
2. Metroplex proposes the V1 backlog grouping as cards (decision 7): 50 tasks, 10 unadmitted
   proposals. Imported tasks enter only under a Yes.
3. When V1 holds no live work, retire its task path; Path A text is removed from the rules in the
   same change.

## War game

| Phase | Failure | Detection | Recovery |
|---|---|---|---|
| 1 | V1 misbehaves on `main` (T3 adapter change) | V1 readback, journal | `checkout codex/paperclip-exit`, restart V1 |
| 1 | Work service will not start (creds, perms, socket path) | unit status, journal | fix and restart; nothing depends on it yet |
| 2 | Approver id wrong: every Yes fails `APPROVER_NOT_RECOGNIZED` | bot reply after Yes | correct env, restart work service; card stays waiting |
| 2 | Qwen down or slow | reasoning breaker message; cards take minutes | Jev-confident routes continue; `metroplex reset reasoning` |
| 2 | Status flipped but daemon not running | `/status` silent | start unit, or flip the line back |
| 2 | Wrong agent owns a project | card screen before Yes | Edit before Yes; after Yes cancel the project via operator |
| 3 | Host double-executes or ignores stop | Teletraan attempt receipts, stop proofs | stop host unit; Teletraan fencing rejects stale attempts |
| 3 | A worker tries a reserved step | `RESERVED_ACTION_REQUIRES_APPROVAL`, publish refusal | none needed; capability absence is the wall |
| 4 | Backlog import floods routing | wake counts, sweep caps | `/pause`; import in smaller card groups |

## Rollback (any phase)

`/stop` in the bot (pauses every Metroplex project, exits); `systemctl --user stop metroplex
teletraan-work`; flip the status line back to "B is NOT ACTIVE"; if V1 is affected, check out
`codex/paperclip-exit` and restart `teletraan.service`. Work state in `work.sqlite` and the V1
store is untouched by rollback.
