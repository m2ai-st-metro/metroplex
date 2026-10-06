Supersedes proposal: Q-20261004-033752-devastator-live-completion. Matthew directed this filing on 2026-10-04 after reviewing the redrafted card. Use this text, not the superseded proposal's wording, when preparing the Metroplex card.

Title: Where the side-walk returned: live Digital Maker-style production app (Devastator pilot)

Source: proposal Q-20261004-033752-devastator-live-completion, corrected by
/home/apexaipc/projects/research/devastator/deliverables/live-application-completion-packet.md
(committed in 79fbdec). This text replaces the proposal JSON's provisional naming and its
"Returned" capitalization.

Names:
- Where the side-walk returned: the application this card delivers. A private,
  noncommercial, original rebuild shaped like Digital Maker's Make an AI Movie journey.
- Devastator: the reverse-engineering method that studied Digital Maker and produced the
  app's specification. This card builds no Devastator code.
- Event Horizon Child: the source film studied. Its story and frozen brief are unchanged.

Objective: Deliver Where the side-walk returned as a persistent interactive application
served from ProBook and used from Matthew's Surface. It turns a creator brief into a
reviewed production plan, consistent exports, and explicit take selection. It builds on
the reviewed offline kit and stays portable across native AI tools.

What the app does:
1. A project page explains the brief, proposed additions, baseline findings, and
   review/readiness state in plain language.
2. Edits brief, characters, voices, locations, vehicles, scenes, shots, camera, dialogue,
   duration, and requirement treatment, with stable IDs. Generated details are shown
   apart from creator-supplied ones.
3. Saves immutable revisions in an ignored project-local data directory. Shows unsaved
   state and restores earlier revisions. Original attachments and frozen evidence stay
   untouched.
4. Generates a context packet for a native AI session and imports a proposed plan, with
   actionable validation errors. An AI response never creates creative approval.
5. Shows stale approvals and the shots/takes they affect after edits. Records real
   creator review decisions with evidence and the current input/plan fingerprints.
   Synthetic demonstration receipts are clearly labeled and kept apart.
6. Exports Markdown/CSV packs into new revision directories through the existing
   exporter, with verified downloads and recovery from an incomplete export. The
   diagnostic-only baseline is kept separate.
7. Imports and selects local takes, replaces or cancels a selection, and shows readiness
   without guessing filenames or judging audiovisual quality from hashes.
8. Keeps optional manual trend records with source/metric uncertainty and works fully
   without them.

Base (app v1, the offline kit): commit ae61f6763da5204f6feca38afe4e4cce73483575,
attempt p-devastator-offline-film--0734:implementation-full:input-role-binding-recovery:a1:rework:2:rework:3
(completed-unintegrated), spec revision 2 SHA256
7987cfa9fca264ec17182d7823556ffc8bb73c1bb130d97d8286d64b9b6b4e7c. The kit's validation,
freshness checks, deterministic exports, take selection, and readiness contracts are
preserved, not duplicated or weakened.

Goal 1: Build on the existing reviewed kit result as the app's base and record its
current acceptance status in the delivery evidence. Accepting that result stays in
project p-devastator-offline-film--0734 with its task owner; this card neither accepts
nor settles it. Preserve the original result and frozen inputs.

Goal 2: Implement and verify the app's eight workflows above on top of the kit, with
persistent revisions and recovery.

Goal 3: Deliver the app at a verified Surface-reachable ProBook LAN address,
demonstrate the real browser workflow, complete independent review, and verify the
handoff/restart/backup instructions.

Architecture: small TypeScript HTTP backend plus browser frontend that reuses kit
modules. The API accepts only constrained actions and project-relative identifiers: no
shell execution, no unrestricted paths, no credential files. State-changing requests
reject cross-origin calls. Allowed hosts are limited to the configured local/LAN
addresses. Request and upload sizes are bounded, and imported text is sanitized before
display. Check port collisions and the actual network address; never assume an old IP or
localhost. MCP only if a demonstrated operation needs it.

Done when:
- The existing offline kit command still passes in a fresh offline checkout.
- Focused API tests cover valid edits, invalid imports, stale approval, revision
  recovery, export collision/interruption, path containment, and rejected cross-origin
  mutations.
- A real browser demonstration edits a plan, saves and reloads it, shows staleness,
  restores a revision, and verifies a clearly synthetic example export download.
- The Surface-reachable URL, launch/restart steps, persistence location, backup
  instructions, exact source revision, screenshots, and known limits are documented.
- Independent review covers the actual final UI/backend change and original requirement
  coverage. The README accurately describes delivered behavior and what still needs
  human creative review.

Devastator method evidence (recorded, not built): this pilot also closes Devastator's
first milestone by showing an original implementation, independent review, and a second
native session reproducing the app workflow from the handoff alone, without the
originating chat.

Writable scope: additive app source, tests, docs, and delivery evidence in
/home/apexaipc/projects/research/devastator; ignored project-local runtime state and
generated exports. Isolated assigned checkout only. No changes to shared skills,
work-service routing, original attachments, or historical frozen evidence (spec revision
2 and its receipts stay as history).

Reserved effects: public publication/push/deployment, paid generation/purchases,
external contact, and live fleet configuration need separate approval. The intended
delivery surface is the local attended browser preview. Any persistent host-service
change must be specified before activation.

Out of scope: a rendered movie, Flow generation, Digital Maker account or import
compatibility, paid generation, public distribution, and autonomous fleet changes.
Mock clips and synthetic receipts are never labeled as a finished film; finishing a real
film is a separate creative/media deliverable.

This card text is inert until submitted and approved through the current Metroplex path.
Existing project grants are not silently expanded by it.