from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from . import blocks as B
from . import resume_pdf
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
        posted = post(p.channel, blocks, f"Apply pack: {p.title} at {p.company}", attachments)
        _attach_resume(p, posted)
        return posted

    def _attach_resume(p: PackIn, posted: PostedOut) -> None:
        """Render the selected claims as a resume and put it in the thread.

        In the thread rather than the message so the pack still reads as one
        block, and best-effort throughout: a PDF that fails to render or upload
        must not fail the pack that was already posted and is already correct.

        A rejected pack produces no file — resume_pdf.build returns None — so
        the citation check cannot be walked around by downloading the draft.
        """
        try:
            markdown = (cfg.resume_source.read_text()
                        if cfg.resume_source.exists() else "")
            path = resume_pdf.build(p, candidate_name=p.candidate_name or "Candidate",
                                    out_dir=cfg.resume_dir, resume_md=markdown)
        except Exception as exc:
            print(f"[resume] could not render: {type(exc).__name__}: {exc}")
            return
        if path is None:
            return
        try:
            client.files_upload_v2(
                channel=posted.channel, thread_ts=posted.ts, file=str(path),
                filename=path.name, title=f"{p.candidate_name or 'Resume'} — {p.title}",
                initial_comment=":page_facing_up: Resume for this role — every line a claim you verified.",
            )
        except SlackApiError as exc:
            # files:write is a separate scope; without it the app has to be
            # reinstalled. Say where the file is rather than losing it.
            detail = exc.response.get("error", "")
            client.chat_postMessage(
                channel=posted.channel, thread_ts=posted.ts,
                text=(f":page_facing_up: Resume written to `{path}`.\n"
                      f"_Slack upload needs the `files:write` scope "
                      f"({detail}) — add it to manifest.yaml and reinstall the app._"))

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
