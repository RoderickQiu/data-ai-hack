import json
from typing import Any

from slack_bolt import App

from . import blocks as B
from .schemas import PackIn
from .signals import Signal, SignalSink

PACKS: dict[str, PackIn] = {}


def _val(body: dict[str, Any]) -> dict[str, Any]:
    return json.loads(body["actions"][0]["value"])


def _replace_actions(client, body: dict[str, Any], block_id: str, replacement: dict[str, Any]) -> None:
    message = body.get("message")
    if not message:
        return
    new_blocks = [replacement if b.get("block_id") == block_id else b for b in message["blocks"]]
    client.chat_update(channel=body["channel"]["id"], ts=message["ts"], blocks=new_blocks, text=message.get("text", ""))


def register(app: App, sink: SignalSink) -> None:
    def record(kind: str, v: dict[str, Any], user: str, **extra) -> None:
        sink.emit(
            Signal(
                kind=kind,
                run_id=v.get("r"),
                day=v.get("d"),
                job_id=v.get("j"),
                slack_user=user,
                **extra,
            )
        )

    @app.action("keep")
    def on_keep(ack, body, client):
        ack()
        v = _val(body)
        record("keep", v, body["user"]["id"])
        _replace_actions(client, body, f"act::{v['j']}", B.decided_context("keep"))

    @app.action("skip")
    def on_skip(ack, body, client):
        ack()
        v = _val(body)
        record("skip", v, body["user"]["id"])
        _replace_actions(client, body, f"act::{v['j']}", B.decided_context("skip"))

    @app.action("not_for_me")
    def on_not_for_me(ack, body, client):
        ack()
        v = _val(body)
        v["ch"] = body["channel"]["id"]
        v["ts"] = body["message"]["ts"]
        client.views_open(trigger_id=body["trigger_id"], view=B.not_for_me_modal(v))

    @app.view("not_for_me_submit")
    def on_not_for_me_submit(ack, body, client, view):
        ack()
        v = json.loads(view["private_metadata"])
        state = view["state"]["values"]
        tags = [o["value"] for o in state["reasons"]["tags"].get("selected_options") or []]
        text = state["note"]["text"].get("value")
        record("not_for_me", v, body["user"]["id"], reason_tags=tags, reason_text=text)
        fake = {"channel": {"id": v["ch"]}, "message": _fetch_blocks(client, v["ch"], v["ts"])}
        _replace_actions(client, fake, f"act::{v['j']}", B.decided_context("not_for_me", tags))

    @app.action("request_pack")
    def on_request_pack(ack, body, client):
        ack()
        v = _val(body)
        record("request_pack", v, body["user"]["id"])
        _replace_actions(client, body, f"act::{v['j']}", B.decided_context("request_pack"))

    @app.action("report_applied")
    def on_applied(ack, body, client):
        ack()
        v = _val(body)
        record("report_reply", v, body["user"]["id"], payload={"event": "applied"})
        client.chat_postMessage(
            channel=body["channel"]["id"],
            thread_ts=body["message"]["ts"],
            text=f":outbox_tray: Logged — you applied to {v.get('t')} at {v.get('c')}. I'll chase it in 7 days.",
        )

    @app.action("answer_questions")
    def on_answer_questions(ack, body, client):
        ack()
        v = _val(body)
        pack = PACKS.get(v["j"])
        if not pack:
            client.chat_postEphemeral(
                channel=body["channel"]["id"], user=body["user"]["id"], text="That pack is no longer in memory — repost it."
            )
            return
        meta = json.dumps({"j": v["j"], "r": v["r"], "ch": body["channel"]["id"], "ts": body["message"]["ts"]})
        client.views_open(trigger_id=body["trigger_id"], view=B.questions_modal(pack, meta))

    @app.view("questions_submit")
    def on_questions_submit(ack, body, view, client):
        ack()
        meta = json.loads(view["private_metadata"])
        answers = {
            block_id.removeprefix("q::"): next(iter(vals.values())).get("value")
            for block_id, vals in view["state"]["values"].items()
        }
        for question_id, text in answers.items():
            sink.emit(
                Signal(
                    kind="answer",
                    run_id=meta["r"],
                    job_id=meta["j"],
                    slack_user=body["user"]["id"],
                    payload={"question_id": question_id, "answer": text},
                )
            )
        client.chat_postMessage(
            channel=meta["ch"],
            thread_ts=meta["ts"],
            text=f":brain: Saved {len(answers)} answer{'s' if len(answers) != 1 else ''}. I won't ask again.",
        )

    @app.action("answer_one")
    def on_answer_one(ack, body, client):
        ack()
        v = _val(body)
        v["ch"] = body["channel"]["id"]
        v["ts"] = body["message"]["ts"]
        client.views_open(trigger_id=body["trigger_id"], view=B.single_question_modal(v))

    @app.view("single_question_submit")
    def on_single_question_submit(ack, body, view, client):
        ack()
        v = json.loads(view["private_metadata"])
        answer = view["state"]["values"]["single"]["answer"].get("value")
        sink.emit(
            Signal(
                kind="answer",
                run_id=v.get("r"),
                job_id=v.get("j"),
                slack_user=body["user"]["id"],
                payload={"question_id": v.get("q"), "answer": answer},
            )
        )
        client.chat_postMessage(channel=v["ch"], thread_ts=v["ts"], text=":brain: Saved. I won't ask again.")

    @app.action("pref_confirm")
    def on_pref_confirm(ack, body, client):
        ack()
        v = _val(body)
        sink.emit(
            Signal(kind="confirm_preference", run_id=v.get("r"), slack_user=body["user"]["id"], payload=v)
        )
        _replace_actions(
            client, body, f"pref::{v['p']}", B.note((":white_check_mark: Active — reranking now"))
        )

    @app.action("pref_reject")
    def on_pref_reject(ack, body, client):
        ack()
        v = _val(body)
        sink.emit(Signal(kind="reject_preference", run_id=v.get("r"), slack_user=body["user"]["id"], payload=v))
        _replace_actions(
            client, body, f"pref::{v['p']}", B.note((":x: Dropped — I won't suggest this again"))
        )

    @app.action("autonomy_on")
    def on_autonomy_on(ack, body, client):
        ack()
        v = _val(body)
        sink.emit(Signal(kind="grant_autonomy", run_id=v.get("r"), slack_user=body["user"]["id"], payload=v))
        _replace_actions(
            client,
            body,
            f"auto::{v['d']}",
            B.note(f":robot_face: Autonomous for *{v['d']}* — I'll tell you what I did, not ask"),
        )

    @app.action("autonomy_later")
    def on_autonomy_later(ack, body, client):
        ack()
        v = _val(body)
        sink.emit(Signal(kind="defer_autonomy", run_id=v.get("r"), slack_user=body["user"]["id"], payload=v))
        _replace_actions(
            client,
            body,
            f"auto::{v['d']}",
            B.note(":pause_button: Staying supervised — I'll ask again after 10 more decisions"),
        )

    @app.action("claim_confirm")
    def on_claim_confirm(ack, body, client):
        ack()
        v = _val(body)
        sink.emit(Signal(kind="verify_claim", run_id=v.get("r"), slack_user=body["user"]["id"], payload=v))
        _replace_actions(
            client, body, f"claim::{v['cl']}", B.note((":white_check_mark: Verified — usable in packs"))
        )

    @app.action("claim_discard")
    def on_claim_discard(ack, body, client):
        ack()
        v = _val(body)
        sink.emit(Signal(kind="discard_claim", run_id=v.get("r"), slack_user=body["user"]["id"], payload=v))
        _replace_actions(
            client, body, f"claim::{v['cl']}", B.note((":wastebasket: Discarded"))
        )

    @app.action("open_doc")
    def on_open_doc(ack):
        ack()

    @app.action("open_sheet")
    def on_open_sheet(ack):
        ack()

    @app.event("app_mention")
    def on_mention(event, say):
        say(
            thread_ts=event.get("ts"),
            text="I post a ranked digest once a day. Tap *Keep*, *Not for me* or *Skip* on any role — "
            "*Not for me* is the one that teaches me something.",
        )


def _fetch_blocks(client, channel: str, ts: str) -> dict[str, Any]:
    res = client.conversations_history(channel=channel, latest=ts, limit=1, inclusive=True)
    messages = res.get("messages") or [{}]
    return messages[0]
