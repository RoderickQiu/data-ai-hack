from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from slack_sdk import WebClient

from . import blocks as B
from .config import Config
from .handlers import PACKS, QUESTIONS, SLATES
from .schemas import AutonomyIn, ClaimsIn, DigestIn, PackIn, PostedOut, PreferenceIn, QuestionIn


def create_api(client: WebClient, cfg: Config) -> FastAPI:
    def auth(authorization: str = Header(default="")) -> None:
        if authorization != f"Bearer {cfg.api_token}":
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    api = FastAPI(
        title="Job agent — Slack surface",
        description=(
            "Everything the agent shows a human goes through here, and every human response "
            "comes back out as one Signal on the sink. Post JSON, get back the channel and ts."
        ),
        version="1.0.0",
    )
    guarded = APIRouter(dependencies=[Depends(auth)])

    def post(channel: str | None, blocks: list[dict[str, Any]], text: str, attachments=None) -> PostedOut:
        res = client.chat_postMessage(
            channel=channel or cfg.channel,
            blocks=blocks,
            text=text,
            attachments=attachments or [],
            unfurl_links=False,
        )
        return PostedOut(ok=True, channel=res["channel"], ts=res["ts"])

    @api.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "channel": cfg.channel, "sink": str(cfg.signal_log), "webhook": bool(cfg.signal_webhook)}

    @guarded.post("/digest", response_model=PostedOut)
    def digest(d: DigestIn) -> PostedOut:
        SLATES[d.run_id] = {j.job_id: j.prediction for j in d.jobs if j.prediction}
        return post(d.channel, B.digest_message(d), f"Day {d.day}: {len(d.jobs)} roles for you")

    @guarded.post("/pack", response_model=PostedOut)
    def pack(p: PackIn) -> PostedOut:
        PACKS[p.job_id] = p
        QUESTIONS.update({q.question_id: q.text for q in p.questions})
        blocks, attachments = B.pack_message(p)
        return post(p.channel, blocks, f"Apply pack: {p.title} at {p.company}", attachments)

    @guarded.post("/preference", response_model=PostedOut)
    def preference(p: PreferenceIn) -> PostedOut:
        return post(p.channel, B.preference_message(p), f"Preference to confirm: {p.rule_text}")

    @guarded.post("/autonomy", response_model=PostedOut)
    def autonomy(a: AutonomyIn) -> PostedOut:
        return post(a.channel, B.autonomy_message(a), f"May I run {a.domain} alone?")

    @guarded.post("/claims", response_model=PostedOut)
    def claims(c: ClaimsIn) -> PostedOut:
        return post(c.channel, B.claims_message(c), f"Confirm {len(c.claims)} claims")

    @guarded.post("/question", response_model=PostedOut)
    def question(q: QuestionIn) -> PostedOut:
        QUESTIONS[q.question_id] = q.text
        return post(q.channel, B.question_message(q), q.text)

    api.include_router(guarded)
    return api
