---
name: meeting-room
description: >-
  Use and run the PERMANENT business-unit meeting rooms: one claude.ai artifact
  per business unit (Bubble Portfolio BU, Méthodes IA BU, Clients pros BU) where
  Joris (the operator) meets the agents of that unit in one live page, with the
  agenda and the decisions he validates on the left, the conversation in the
  middle and the validated outputs on the right. Use whenever Joris wants to
  "meet", "open the room", "get everyone around the table on <BU/project>", when
  you receive a "MEETING POLL" / "meeting mode" / "join the room" instruction,
  when you must post a decision for Joris to validate or an output to review in a
  room, or when a new business unit needs its own room.
---

# Business-unit meeting rooms

Each business unit has ONE permanent room: a claude.ai artifact page whose
content lives in the page database. Rooms are permanent (Joris, Telegram 10144,
2026-09-29); only the *meetings* in them are occasional. The list of rooms is in
`rooms.yaml` (BU, name, URL, chair, members). The cockpit home links them (section
"Salles des business units"), and Tony's Chantiers page links them too.

Origin: first live meeting 2026-09-29 (Bubble Portfolio review with Tony, Ben,
Miranda and Rick, Telegram 10103–10145).

## The shared format (every room)

| Left | Middle | Right |
|---|---|---|
| **Decisions for Joris to validate** (as they come up during the conversation) + **the agenda** (for Fonds: the pipeline, step by step, with live status) | **Conversation** (Joris types, tags; agents answer signed) | **Validated outputs** (what each mission really produced, reviewed in session, with links) |

Database collections (the page renders them live; agents read and write them
with the `ArtifactData` tool):

- `messages`: `{author, text, to[], ts}`. `author` = lowercase key (`joris`,
  `jade`, `tony`, `ben`, `miranda`, `rick`, `maya`, `tonio`, `geraldine`). `ts` =
  **real clock** at write time (`date -u +%Y-%m-%dT%H:%M:%SZ`), never estimated.
  doc_id `m-<UTC yyyymmddThhmmss>-<author>`.
- `decisions`: `{order, title, context, options[], reco, status, answer, link, link_label, decided_at}`.
  An agent posts a decision with `status: "open"`, 2–4 short `options`, and the
  index of its recommendation in `reco`. Joris clicks an option on the page, which
  writes `status: "done"` + `answer` + `decided_at`. Agents act on `answer`, never
  on a decision still open.
- `pipeline` (the agenda): `{order, layer, label, owner, status, note}` with
  `status` ∈ `todo | review | decide | validated | blocked`. The chair keeps it
  current during the meeting.
- `outputs`: `{order, owner, step, status, title, summary, link, link_label}`.
  Only real outputs (a file, a PR, a published post) with a working https link;
  `status: validated` once Joris or the chair accepted it.

Never rewrite someone else's document; create new ones (last-writer-wins).

## Roles

- **Chair** (in `rooms.yaml`; Tony for Fonds and Clients pros, Rick for Méthodes
  IA): sets the agenda (`pipeline`), gives the floor, turns open questions into
  `decisions`, moves steps to `validated`, and summarises at the end.
- **Members**: answer when tagged, post their real outputs to `outputs`, and ask
  for decisions through `decisions` (not buried in a chat message).
- **Joris**: types, tags, and validates decisions with one click. A decision
  validated in a room counts as his decision *for that room's scope*. Actions that
  need a gate (publishing, trading, merging PRs, spending) still go through their
  normal gate (cockpit, Telegram, PR approval).

## During a meeting (poll cadence)

Nothing wakes an agent on a database write, so during a meeting each member polls:

- **chair**: `CronCreate` `*/2 * * * *`; **others**: `*/5 * * * *`; whoever has
  the floor switches to `*/2` until their step is closed, then back to `*/5`.
- Poll prompt: `MEETING POLL: read new messages in the room <URL> and answer if tagged`.
- Each poll: `ArtifactData` `query` on `messages` where `ts >` the last one you
  handled; also re-read `decisions` you are waiting on. Answer only messages
  containing `@<your key>` or `@all` not written by you; sign `— Name`. Nothing
  for you: end the turn immediately (no acknowledgement messages).
- **Meeting over** (Joris says so, or after 3 hours): `CronDelete` the poll job.
  The room stays; its content remains the record.

Cost: every poll is a model turn over your whole (cached) context, whether or not
you are tagged. Keep meetings bounded and never leave a poll running afterwards.

## Outside meetings

Rooms are also the BU's standing board. Without polling, at your normal loop:
post a real output to `outputs` when a BU mission produces one, and a decision to
`decisions` when you need Joris's call on BU matters. Joris sees them next time he
opens the room.

## Inviting agents to a meeting

The chair (or Rick) sends each member a one-time message in its own session with
the room URL, its role (chair or member) and the poll cadence (fleet
`telegram-inject` / `telegram-message-a2a` skills; signed A2A where the receiver
requires it, e.g. Miranda). An agent that restarts mid-meeting is invited again.

## Creating a room for a new business unit

1. Copy `assets/room.html`; replace `{{ROOM_TITLE}}` (e.g. "Clients pros BU"),
   `{{BU_LABEL}}`, `{{AGENDA_TITLE}}`, `{{AGENDA_SUB}}`, `{{PEOPLE}}` (participant
   chips) and `{{CHIPS}}` (tag buttons).
2. Publish with the Artifact tool, `capabilities: {"db": {}, "user": {}}`, icon `chat`.
3. One `ArtifactData` `list` on `messages` to confirm the database answers.
4. Add it to `rooms.yaml` and to `console/templates/partials/_bu_rooms.html`
   (PR), and ask Tony to link it from the Chantiers page.
5. Sharing: rooms are private to the owning account. An agent on another account
   (e.g. on Jade's Mac) can only open it once Joris shares it from the Share menu.

## Why comments are not used

Artifact comments wake only the session that published the page; the first reply
to each comment is posted automatically and unsigned by whichever armed session
reacts first; every reply shows as "Claude · via Joris". In the first room this
made Ben answer a message meant for Tony. The rooms therefore use the database.

## Gotchas

- A `CronCreate` job only fires while the session is idle; a long mission run
  delays answers. Say so if Joris is waiting on you.
- Don't store secrets or client personal data in a room: anyone the page is shared
  with can read it all.
- Deleting a room needs Joris's own confirmation (`/artifacts`, then `d`, or the
  page menu). Agents never delete a permanent room.
