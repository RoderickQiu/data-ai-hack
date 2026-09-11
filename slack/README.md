# Slack surface

Every point where a human touches the agent. Posts digests, apply-packs,
preference prompts and autonomy requests into Slack; turns every click back into
one `Signal` on a sink the memory layer reads.

Carries proof moments **P1** (questions fall to zero), **P2** (preference
confirmed, shortlist visibly reorders), **P5** (gaps flagged, never papered
over) and **P6** (autonomy earned and switched on) from `DESIGN.md` §2.

## Two decisions worth knowing before you read the code

**Socket Mode, not HTTP request URLs.** `DESIGN.md` §4 schedules an hour-3
go/no-go on whether a Slack button click can round-trip into a pipeline, and §11
keeps "Slack interactivity does not round-trip" as a live risk. Both assume an
HTTP-mode app, which needs a public request URL for every interaction. Socket
Mode dials outbound over a WebSocket instead: no tunnel, no URL verification, no
captive-wifi failure on stage. **The go/no-go can be closed early and the
numbered-reply fallback is not needed.**

**This process owns no data.** It takes payloads in over a small HTTP API and
emits human responses to a sink. Nothing here reads Cognee, HydraDB or hotdata,
so the whole surface is buildable and rehearsable against fixtures while the
other layers are still being written.

```
RocketRide / MCP  --POST /digest,/pack,...-->  slackbot  --Signal-->  data/signals.jsonl
                                                   |                        |
                                                 Slack  <--clicks--  human  +--> SIGNAL_WEBHOOK_URL
```

## See the UI before you have a Slack app

```sh
cd slack && python -m slackbot.preview
```

Prints a Block Kit Builder link for each surface. No tokens needed.

## Setup

**1. Create the app.** <https://api.slack.com/apps> → *Create New App* → *From a
manifest* → pick your workspace → paste `manifest.yaml` → Create.

**2. Get the two tokens.**

- *Basic Information* → *App-Level Tokens* → *Generate Token and Scopes* → name
  it anything, add the **`connections:write`** scope → Generate. That is
  `SLACK_APP_TOKEN`, starting `xapp-`. Socket Mode does not connect without it.
- *Install App* → *Install to Workspace* → Allow. The *Bot User OAuth Token* is
  `SLACK_BOT_TOKEN`, starting `xoxb-`.

**3. Pick a channel.** Create one (`#job-agent`), then *Channel details* →
bottom of the About tab → copy the channel ID (`C…`). Invite the bot with
`/invite @Job Agent`.

**4. Fill the env.**

```sh
cp .env.example .env   # then paste the two tokens and the channel id
```

**5. Install and run.**

```sh
cd slack
pip install -r requirements.txt
python -m slackbot.app
```

**6. Prove it works.** In a second terminal:

```sh
cd slack
python -m slackbot.demo cold   # run 1: near-random predictions, pack asks 6 questions
python -m slackbot.demo warm   # run 19: replayed, explained, asks nothing
```

Click the buttons. Each click updates the message in place and appends a line to
`data/signals.jsonl`.

## Inbound API

Interactive docs at <http://127.0.0.1:8765/docs>. Every endpoint takes
`Authorization: Bearer $SLACK_API_TOKEN` and returns `{ok, channel, ts}`.

| Endpoint | Posts | Drives |
|---|---|---|
| `POST /digest` | Ranked roles with the "why" paragraph, prediction badge and Keep / Not for me / Skip / Prepare pack | pipeline P-A |
| `POST /pack` | Apply pack: tailored summary, cited claims, gaps in a red-barred attachment, questions button | pipeline P-B |
| `POST /preference` | "I think you prefer companies under 2,000 people" + Confirm / Not quite | P2 |
| `POST /autonomy` | "You've agreed with 13 of my last 15 calls" + Turn it on / Not yet | P6 |
| `POST /claims` | Unverified claims to Confirm or Discard | §6.1 |
| `POST /question` | One `ask_human` question | §6.2 |
| `GET /health` | Channel, sink path, whether a webhook is configured | — |

Payload shapes are in `slackbot/schemas.py`; realistic examples are in `fixtures.py` and
are what `/docs` shows.

## Outbound signals

One envelope for everything a human does, appended to `data/signals.jsonl` and
POSTed to `SIGNAL_WEBHOOK_URL` when set:

```json
{"kind": "not_for_me", "run_id": "run-012", "day": "11", "job_id": "lev-ramp-882",
 "reason_tags": ["company_too_large"], "reason_text": null, "payload": {},
 "slack_user": "U123", "at": "2026-09-11T14:02:11+00:00", "human_touches": 1}
```

`kind` is one of `keep`, `skip`, `not_for_me`, `request_pack`, `answer`,
`confirm_preference`, `reject_preference`, `grant_autonomy`, `defer_autonomy`,
`verify_claim`, `discard_claim`, `report_reply`.

Two things follow from this file being the only place humans touch the system:

- It is the authoritative source for **line 3 of the chart**. `questions_asked`
  is `count(kind='answer')` grouped by `run_id`; `human_touches` is `count(*)`.
- Only `not_for_me` carries `reason_tags`, matching `DESIGN.md` §6.3 — `skip`
  means "not now" and must never become evidence of a preference.

The JSONL is written before the webhook is attempted, so a webhook outage
delays the memory write but never loses the signal.

## Handing over to the memory layer

Set `SIGNAL_WEBHOOK_URL` to any endpoint accepting `POST` of one JSON object.
That is the whole integration — no shared library, no import.

## Open, deliberately

- **Pack state is in-process.** `slackbot/handlers.py`'s `PACKS` holds posted packs so the
  questions modal can rebuild itself. A restart loses pending question modals;
  reposting the pack fixes it. Persisting it is not worth the hour.
- **No retry on `chat_update`.** A rate-limited update leaves the buttons live
  and the signal is still recorded, so the worst case is a double click.
- **Exposing the API beyond localhost needs a real `SLACK_API_TOKEN`.** It
  defaults to `dev-local-token` and binds to `127.0.0.1`.
