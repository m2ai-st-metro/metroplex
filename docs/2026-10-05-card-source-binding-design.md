# Card source binding: carry the proposal's full scope to the worker

Status: proposal for Matthew's review. Nothing here is implemented except the
minimal fixes listed under "Shipped with this note".

## The invariant we want

What Matthew taps Yes on must bind the full text of the proposal it came from,
and every worker attempt in the granted project must receive that text. Today the
card is a lossy model summary, and the summary is the only thing that travels.

## Evidence (read-only, 2026-10-05)

Proposal `Q-20261005-045218-sidewalk-app-card` (work service) became
`card-cc97709c` (`data/cos.db`) and project `p-where-the-side-walk-retu-709c`.

| Proposal content | On the approved card | In tasks / worker context |
|---|---|---|
| Done when: 5 bullets | 1 bullet (regex bug, fixed) | 1 bullet's worth |
| 8 named workflows | "eight workflows", none named | none named |
| Architecture: no shell exec, path containment, cross-origin rejection, allowed hosts, bounded sizes, sanitized imports, port check | absent | absent |
| Base: commit `ae61f67`, attempt id, spec rev 2 SHA256 | absent | absent |
| Goal 3: independent review, handoff/restart/backup verified | dropped from g3 | dropped |
| Devastator method evidence (second native session reproduces) | absent | absent |
| Writable scope: `/home/apexaipc/projects/research/devastator`, additive only | prose in `scopeIn` | task has none; contribution `writableScope: []` |

The first attempt's `context` (`t1:created:1:a1`) is 789 characters of JSON: task
objective, acceptance, deliverable, empty `writableScope`. The proposal body is
6,630 bytes. `CARD_SYSTEM` has no field for constraints, architecture, or workflow
lists, so the model had nowhere to put them.

## Proposal

```
proposal (write-once, in work service)
   | id + sha256(body)
   v
card.source = {kind, id, sha256}   <- inside card_digest, so Yes binds it
   |
   +--> decompose prompt gets the source text (tasks can cite it)
   |
   v
project.card (Teletraan stores the card as-is)
   |
   v
attempt.queue context.card = {title, doneWhen, scopeIn, scopeOut, source}
   |
   v
ttn run: re-read proposal, check sha256, write /run/ttn/input/source.md
         (mismatch or missing: fail closed, SOURCE_DRIFT)
```

### A. Bind the source into the card digest

- New card field `source: {"kind": "proposal", "id": "<proposal id>", "sha256": "<hex of UTF-8 body>"}`.
  `card_digest` already hashes every field, so the Yes covers it with no hashing change.
- Every card gets a proposal. `card <id>` already starts from one. A Telegram
  objective is filed as a proposal first through the same `proposal.file` path
  `_file_note` uses, then drafted from it. One source kind, one code path.
- Proposals cannot change after filing (`proposal.file` is the only proposal op
  and is `fresh()`-guarded, `work.mjs:320`), so the hash can only fail on a wrong id.
- `render` shows `Source: <id> (sha256 <first 12>); its full text binds the work.`
  and `RENDERED_KEYS` gains `source`, keeping the B1 rule (every hashed field is
  on screen).
- Teletraan: `validateCard` already clones unknown fields (`work.mjs:34`), so
  `project.card` stores `source` today. Add a shape check there and in
  `spec.validate_card` (both layers, neither trusts the other).

### B. Pass the source to the worker

- Teletraan `attempt.queue` adds `card: {title, doneWhen, scopeIn, scopeOut, source}`
  and the task's goal to `context` (`work.mjs:494`).
- `ttn run` resolves `context.card.source.id`, checks the sha256, and writes
  `/run/ttn/input/source.md`. The run prompt (`run.mjs:117`) gains one line:
  "source.md is the approved source; its constraints bind you. task.json says
  which part of it is yours."
- The decompose prompt receives the source text, so tasks can name the specific
  workflows and constraints instead of "the eight workflows".
- Size: a 6.6 KB source is far inside Qwen's 16k context and the worker's budget.

### C. scopeIn to writableScope

`scopeIn` is prose and cannot be mapped mechanically. Teletraan wants relative,
normalized paths per contribution (`work.mjs:451-457`) and refuses overlapping
scopes between live attempts in one project (`work.mjs:487-490`). So:

- Card gains `writableScope: ["<relative path>", ...]` (relative to the clone
  root). The model drafts it from the proposal's writable-scope text; code
  validates it (relative, no `..`) and the card renders it, so the Yes binds it.
  The absolute repo stays in `scopeIn` prose until a card names a repo field.
- Decomposition gives each task a subset as `spec.writableScope`. Teletraan's
  `validateTaskSpec` whitelists fields (`work.mjs:60-69`) and must accept it.
- Routing passes `task.spec.writableScope` into `contribution.create`
  (`routing.py:359`).
- Trade-off: the card-wide scope on every task would serialize parallel tasks
  through `WRITABLE_SCOPE_CONFLICT`. Per-task subsets keep parallelism; an empty
  list keeps today's unrestricted behavior.

## Decisions for Matthew

1. File every Telegram objective as a proposal before drafting (A, second bullet)? Recommended.
2. When decomposition omits a task's `writableScope`: unrestricted (today) or the card-wide scope (safer, serializes)?
3. `p-where-the-side-walk-retu-709c` was granted from the lossy card. A grant is
   never silently expanded, so carrying the full source there needs a fresh card
   and Yes once A and B land. Its t2 still carries the wrong `external_contact`
   tag; the code fix does not rewrite stored tasks.

## Shipped with this note (minimal, in `cos/intake.py`)

- `stated_done_when` returns the whole done-when block (all bullets and wrapped
  lines up to a blank line), and `_draft` sets the card's `doneWhen` to that block
  verbatim on a first draft instead of trusting "use it as written" to the model.
  An Edit answer still lets the model change it.
- `infer_reserved` matches keywords at a word start (`mechanisms` is no longer `sms`).
- Card and task titles are cut at a word boundary and end with an ellipsis.
