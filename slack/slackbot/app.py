import threading

import uvicorn
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .api import create_api
from . import demo_commands
from .config import Config, ConfigError, load_config
from .handlers import register
from .signals import SignalSink


def build(cfg: Config) -> tuple[App, SignalSink]:
    app = App(token=cfg.bot_token, raise_error_for_unhandled_request=False)
    sink = build_sink(cfg)
    register(app, sink)

    # Every demo beat, triggered by @mentioning the bot. The stores come off
    # the sink so the triggers work exactly when the backend does, and a
    # fixtures-only bot still starts and says so rather than failing to boot.
    demo_commands.register(
        app, cfg,
        lambda: ((sink.insight, sink.store)
                 if getattr(sink, "store", None) is not None else None),
    )
    return app, sink


def build_sink(cfg: Config) -> SignalSink:
    """Fixtures by default; the real agent when SLACK_BACKEND is set.

    The fixture path stays available on purpose. If hotdata or the graph is
    down an hour before the pitch, the surface still demonstrates, and that is
    worth more than making the wiring mandatory.
    """
    if not cfg.backend:
        print("[sink]   fixtures only - set SLACK_BACKEND=1 to write to the agent")
        return SignalSink(cfg.signal_log, cfg.signal_webhook, cfg.signal_webhook_token)

    from insight.store import Insight
    from memory.graph import GraphStore

    from .live import BackendSink

    remember = None
    try:
        from memory.remember import Remember
        remember = Remember()
    except Exception as exc:
        print(f"[sink]   Cognee unavailable, continuing without it: {exc}")

    print("[sink]   every click writes through to the agent's memory")
    return BackendSink(cfg, store=GraphStore(), insight=Insight(), remember=remember)


def main() -> None:
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}")
    app, _ = build(cfg)

    handler = SocketModeHandler(app, cfg.app_token)
    threading.Thread(target=handler.start, daemon=True).start()
    print(f"[slack] socket mode connected, posting to {cfg.channel}")

    api = create_api(app.client, cfg)
    print(f"[api]   http://{cfg.api_host}:{cfg.api_port}/docs")
    uvicorn.run(api, host=cfg.api_host, port=cfg.api_port, log_level="warning")


if __name__ == "__main__":
    main()
