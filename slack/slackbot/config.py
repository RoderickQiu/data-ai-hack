import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from agent.net import hostname

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(RuntimeError):
    pass


def _host(value: str) -> str:
    try:
        return hostname(value, "SLACK_API_HOST")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _confine(path: Path, name: str) -> Path:
    """Every path we take from the environment has to land inside the repo.
    Without this a `../..` in SIGNAL_LOG or RESUME_DIR is a write primitive
    anywhere the process can reach - CWE-23, and Snyk is right to flag it."""
    resolved = path.expanduser().resolve()
    if not resolved.is_relative_to(REPO_ROOT):
        raise ConfigError(f"{name} must stay inside {REPO_ROOT}, got {resolved}")
    return resolved


def _resume_source() -> Path:
    """The markdown the PDF takes its structure from. A real resume.md wins
    over the committed example, which is what candidate/README.md tells people
    to do."""
    override = os.getenv("RESUME_SOURCE")
    if override:
        return _confine(_anchor(override), "RESUME_SOURCE")
    candidates = REPO_ROOT / "candidate"
    real = candidates / "resume.md"
    return real if real.exists() else candidates / "resume.example.md"


def _anchor(value: str) -> Path:
    """Relative paths hang off this folder, never the working directory - the
    repo root has its own data/ and the two must not collide."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else LOCAL_ROOT / path


def _resolve(value: str, name: str) -> Path:
    return _confine(_anchor(value), name)


@dataclass(frozen=True)
class Config:
    bot_token: str
    app_token: str
    channel: str
    api_host: str
    api_port: int
    api_token: str
    signal_log: Path
    resume_dir: Path
    resume_source: Path
    signal_webhook: str | None
    signal_webhook_token: str | None
    backend: bool


def load_config() -> Config:
    load_dotenv(REPO_ROOT / ".env")
    load_dotenv(LOCAL_ROOT / ".env", override=True)

    missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL") if not os.getenv(k)]
    if missing:
        raise ConfigError(
            f"missing {', '.join(missing)} in .env - see slack/README.md setup steps 1-4"
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
        # A bare host, because live.call interpolates it into a URL: a value
        # carrying a `/` or an `@` would move the request to another origin and
        # take SLACK_API_TOKEN with it.
        api_host=_host(os.getenv("SLACK_API_HOST", "127.0.0.1")),
        api_port=int(os.getenv("SLACK_API_PORT", "8765")),
        api_token=os.getenv("SLACK_API_TOKEN", "dev-local-token"),
        signal_log=_resolve(os.getenv("SIGNAL_LOG") or "data/signals.jsonl", "SIGNAL_LOG"),
        resume_dir=_resolve(os.getenv("RESUME_DIR") or "data/resumes", "RESUME_DIR"),
        resume_source=_resume_source(),
        signal_webhook=os.getenv("SIGNAL_WEBHOOK_URL"),
        signal_webhook_token=os.getenv("SIGNAL_WEBHOOK_TOKEN"),
        backend=os.getenv("SLACK_BACKEND", "").lower() in ("1", "true", "yes"),
    )
