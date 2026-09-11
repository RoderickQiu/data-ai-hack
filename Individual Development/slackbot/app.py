import threading

import uvicorn
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .api import create_api
from .config import Config, ConfigError, load_config
from .handlers import register
from .signals import SignalSink


def build(cfg: Config) -> tuple[App, SignalSink]:
    app = App(token=cfg.bot_token, raise_error_for_unhandled_request=False)
    sink = SignalSink(cfg.signal_log, cfg.signal_webhook, cfg.signal_webhook_token)
    register(app, sink)
    return app, sink


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
