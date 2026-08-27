# Telegram review-helper contract

Hermes can attach a small, profile-owned feedback keyboard to a text or media
candidate. The active profile supplies the helper executable; candidate output
supplies only an opaque short id. The gateway never accepts an executable path,
working directory, or arguments from model output.

## Configuration

Put the helper under the Telegram profile's `extra` mapping (a top-level
`platforms.telegram.review_helper` mapping is accepted as a convenience):

```yaml
platforms:
  telegram:
    extra:
      review_helper:
        enabled: true
        executable: /opt/humorbank/bin/review-helper
        args: [--profile, humorbank]
        timeout_seconds: 15
        note_max_bytes: 4096
        # Optional, and must be absolute when present.
        # working_directory: /opt/humorbank
```

`executable` must be an absolute executable file. `args` is a fixed list of
strings owned by the profile. Hermes invokes the helper without a shell as:

```text
/opt/humorbank/bin/review-helper --profile humorbank resolve <compact-json-stdin>
/opt/humorbank/bin/review-helper --profile humorbank event   <compact-json-stdin>
/opt/humorbank/bin/review-helper --profile humorbank note    <compact-json-stdin>
```

`note_max_bytes` defaults to 4096 and is hard-capped at 16 KiB.

The request is one compact JSON object on stdin. The helper returns one JSON
object on stdout and should use `{ "ok": true }` for accepted operations;
non-zero exits, malformed responses, timeouts, unknown candidates, expired
bindings, unauthorized actors, and unbound replies are treated as failures.
The gateway only sends a validated candidate id and Telegram delivery or
feedback fields. It does not pass local artifact paths from the candidate
marker to the helper.

Delivery handoff uses `event_type: "delivery"`. Verdict buttons append an event
with `event_type: "feedback"`, `event: "verdict"`, and `verdict`/`action` set
to `funny`, `weak`, `bad`, or `repost`; diagnostic buttons append a separate
`event_type: "reason"` event. The note prompt and bounded reply use
`event_type: "note_prompt"` and `event_type: "note"`. A correction can use the
same `event` operation with a correction event type so the helper keeps the
ledger append-only.

## Marker and keyboard

The canonical marker is:

```text
[[review_candidate_id:abc123]]
```

`[[review_candidate:abc123]]` is accepted for compatibility with early helper
prototypes. The id is limited to 48 ASCII letters, digits, `_`, or `-`; the
directive is removed before delivery. A resolvable candidate receives one
keyboard with `😂 Funny`, `😐 Weak`, `❌ Bad`, and `♻️ Repost`. Callback data is
the bounded `rh:<action>:<id>` form and remains at or below Telegram's 64-byte
limit.

Weak and Bad append the verdict first, then replace the keyboard with the
diagnostic reasons: bad news peg; wrong card/retrieval; wrong subject,
archetype, or context; mechanism lost; unsupported slot/fact; awkward
deterministic render; source joke itself weak; and other/add note. Add note
sends a Telegram force-reply prompt. The reply is bounded in UTF-8 bytes,
stored verbatim by the helper, and consumed as feedback before normal message
routing. It is never dispatched to the conversational agent.

The helper is the authority for candidate state and append-only feedback. Each
callback resolves the candidate again, checks the actor, and records at most
one event. Unknown, expired, unauthorized, or unbound requests fail closed;
duplicate taps are harmless. Corrections should be represented as new helper
events rather than edits to prior events.

Postgen remains separate and unchanged: `[[postgen_candidate_id:<id>]]` and
the `pg:a|r|v:<id>` callback lane continue to use the existing Postgen helper
and registry.
