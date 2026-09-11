import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KINDS = (
    "keep",
    "skip",
    "not_for_me",
    "request_pack",
    "answer",
    "confirm_preference",
    "reject_preference",
    "grant_autonomy",
    "defer_autonomy",
    "verify_claim",
    "discard_claim",
    "report_reply",
)


@dataclass
class Signal:
    kind: str
    run_id: str | None = None
    day: str | None = None
    job_id: str | None = None
    reason_tags: list[str] = field(default_factory=list)
    reason_text: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    slack_user: str | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    human_touches: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SignalSink:
    """Every human response leaves here exactly once.

    The JSONL file is the durable record even when the webhook is down, so the
    human-effort line of the chart can always be rebuilt by grouping on run_id.
    """

    def __init__(self, log_path: Path, webhook: str | None = None, webhook_token: str | None = None):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.webhook = webhook
        self.webhook_token = webhook_token
        self._lock = threading.Lock()

    def emit(self, signal: Signal) -> dict[str, Any]:
        record = signal.as_dict()
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.webhook:
            threading.Thread(target=self._post, args=(line,), daemon=True).start()
        return record

    def _post(self, line: str) -> None:
        headers = {"Content-Type": "application/json"}
        if self.webhook_token:
            headers["Authorization"] = f"Bearer {self.webhook_token}"
        req = urllib.request.Request(self.webhook, data=line.encode(), headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"[sink] webhook delivery failed, signal is still in {self.log_path}: {exc}")
