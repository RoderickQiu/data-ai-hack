"""Every beat of the demo, triggered by @mentioning the bot in Slack.

Built on ``app_mention`` rather than slash commands deliberately: the app
already holds ``app_mentions:read``, so this needs no manifest change and no
reinstall — which matters when the alternative is re-authorising an app
minutes before filming.

    @Job Agent digest        today's ranked roles, with the why and a prediction
    @Job Agent next          advance the clock a day, then the digest
    @Job Agent pack          an apply-pack for the top role, resume PDF attached
    @Job Agent pack <job_id> the same, for a role you name
    @Job Agent autonomy      the request to run shortlisting alone
    @Job Agent claims        claims waiting to be confirmed
    @Job Agent ask           one screening question, answered once and never again
    @Job Agent status        day, claims, rules, autonomy state
    @Job Agent help          this list

Each beat runs on its own thread. Slack wants an acknowledgement within three
seconds and judging a day takes longer than that, so the handler returns at
once and the beat posts when it is ready.
"""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

from .config import Config

HELP = (
    "*Demo triggers* — mention me with any of these:\n"
    "• `digest` — today's ranked roles, each with why it is there and a prediction\n"
    "• `next` — move the clock on a day, then post that day's digest\n"
    "• `pack` — an apply-pack for the top role, with the tailored resume attached\n"
    "• `pack <job_id>` — the same for a role you name\n"
    "• `autonomy` — ask to run shortlisting without checking first\n"
    "• `claims` — confirm the claims it inferred rather than read\n"
    "• `ask` — one screening question, stored so it is never asked twice\n"
    "• `status` — where the agent currently stands"
)


def _first_job(store) -> str | None:
    """The top of the last slate, so `pack` needs no argument on camera."""
    best, best_score = None, -1.0
    for node in store.graph.nodes.values():
        if node.label != "Prediction":
            continue
        score = float(node.props.get("score") or 0)
        if score > best_score:
            best, best_score = node.props.get("job_id"), score
    if best:
        return str(best)
    for node in store.graph.nodes.values():
        if node.label == "Job":
            return node.id.removeprefix("job:")
    return None


def register(app: Any, cfg: Config, store_of: Callable[[], Any]) -> None:
    """``store_of`` returns (insight, store) — lazily, so a bot running on
    fixtures still starts and simply says the backend is off."""

    def run(fn: Callable[[], Any], say, thread_ts: str) -> None:
        def worker() -> None:
            try:
                fn()
            except Exception as exc:
                traceback.print_exc()
                say(thread_ts=thread_ts,
                    text=f":warning: `{type(exc).__name__}` — {str(exc)[:280]}")
        threading.Thread(target=worker, daemon=True).start()

    @app.event("app_mention")
    def on_mention(event, say):
        words = (event.get("text") or "").split()
        # Drop the <@U…> mention itself; what follows is the command.
        args = [w for w in words if not w.startswith("<@")]
        command = (args[0].lower() if args else "help").strip(":,.")
        rest = args[1:]
        ts = event.get("ts")

        if command in ("help", ""):
            say(thread_ts=ts, text=HELP)
            return

        pair = store_of()
        if pair is None:
            say(thread_ts=ts, text=":warning: Backend is off — restart me with "
                               "`SLACK_BACKEND=1` and these triggers light up.")
            return
        insight, store = pair

        from . import live

        if command in ("digest", "day", "today"):
            say(thread_ts=ts, text=":hourglass_flowing_sand: Judging today's roles…")
            run(lambda: live.post_day(cfg, insight, store, advance=False), say, ts)

        elif command in ("next", "tomorrow"):
            say(thread_ts=ts, text=":fast_forward: Moving to the next day…")
            run(lambda: live.post_day(cfg, insight, store, advance=True), say, ts)

        elif command == "pack":
            job_id = rest[0].strip("<>|") if rest else _first_job(store)
            if not job_id:
                say(thread_ts=ts, text="No roles in the graph yet — try `digest` first.")
                return
            say(thread_ts=ts, text=f":package: Preparing a pack for `{job_id}`…")
            from agent.clock import DayClock
            run(lambda: live.post_pack(cfg, insight, store, job_id, DayClock.load().day),
                say, ts)

        elif command == "autonomy":
            from memory.autonomy import D1_SHORTLIST, evaluate
            from . import bridge
            readiness = evaluate(store, D1_SHORTLIST)
            payload = bridge.autonomy_payload(readiness, run_id="demo")
            if payload is None:
                say(thread_ts=ts,
                    text=(f":lock: Not ready to ask yet — {readiness.state}, "
                          f"{readiness.value:.0%} of the last {readiness.window} calls "
                          f"against a {readiness.threshold:.0%} bar. It asks when it has "
                          f"earned it, not when prompted."))
                return
            run(lambda: live.call(cfg, "/autonomy", payload.model_dump()), say, ts)

        elif command == "claims":
            run(lambda: live.post_pending_claims(cfg, store), say, ts)

        elif command in ("ask", "question"):
            from . import bridge
            from .schemas import QuestionIn
            question = " ".join(rest) or "What notice period are you on?"
            payload = QuestionIn(run_id="demo",
                                 question_id=bridge.question_id_for(question),
                                 text=question,
                                 context="Asked once. The answer is stored and reused.")
            run(lambda: live.call(cfg, "/question", payload.model_dump()), say, ts)

        elif command == "status":
            from agent.clock import DayClock
            from memory.answers import known_standard_answers
            from memory.autonomy import D1_SHORTLIST, evaluate
            from memory.prefs import active_rules
            readiness = evaluate(store, D1_SHORTLIST)
            rules = [r.sentence() for r in active_rules(store)]
            say(thread_ts=ts, text="\n".join([
                f"*Day {DayClock.load().day}*",
                f"• {len(store.run('verified_claims', {}))} verified claims",
                f"• {len(known_standard_answers(store))} answers it will not ask twice",
                f"• {len(rules)} confirmed preference" + ("s" if len(rules) != 1 else ""),
                *[f"    – {r}" for r in rules],
                f"• Shortlisting: {readiness.state} — agreed with "
                f"{readiness.value:.0%} of the last {readiness.window}",
            ]))

        else:
            say(thread_ts=ts, text=f"Don't know `{command}`.\n\n{HELP}")
