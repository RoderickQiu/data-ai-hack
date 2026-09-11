import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    bot_token: str
    app_token: str
    channel: str
    api_host: str
    api_port: int
    api_token: str
    signal_log: Path
    signal_webhook: str | None
    signal_webhook_token: str | None


def load_config() -> Config:
    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(LOCAL_ROOT / ".env", override=True)

    missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL") if not os.getenv(k)]
    if missing:
        raise ConfigError(
            f"missing {', '.join(missing)} in .env - see README.md setup steps 1-4"
        )

    app_token = os.environ["SLACK_APP_TOKEN"]
    if not app_token.startswith("xapp-"):
        raise ConfigError("SLACK_APP_TOKEN must be an app-level token starting with xapp-")
    if not os.environ["SLACK_BOT_TOKEN"].startswith("xoxb-"):
        raise ConfigError("SLACK_BOT_TOKEN must be a bot token starting with xoxb-")

    return Config(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        app_token=app_token,
        channel=os.environ["SLACK_CHANNEL"],
        api_host=os.getenv("SLACK_API_HOST", "127.0.0.1"),
        api_port=int(os.getenv("SLACK_API_PORT", "8765")),
        api_token=os.getenv("SLACK_API_TOKEN", "dev-local-token"),
        signal_log=Path(os.getenv("SIGNAL_LOG", LOCAL_ROOT / "data" / "signals.jsonl")),
        signal_webhook=os.getenv("SIGNAL_WEBHOOK_URL"),
        signal_webhook_token=os.getenv("SIGNAL_WEBHOOK_TOKEN"),
    )
