"""One place that decides whether a URL is safe to send a credential to.

Every outbound call in this repo carries a bearer token, and every target is
named by an operator — a ``--webhook`` flag, ``ROCKETRIDE_URI``, ``MCP_ENDPOINT``
in ``.env``. urllib will open ``file://`` or ``ftp://`` from the same call it
opens ``https://`` with, so a typo'd env var is a local file read with an API key
attached to it. The check belongs here rather than at each call site, because
the interesting failure is the call site that forgot.

http is allowed alongside https: the tunnel is sometimes plain http in the room,
and that is a deliberate demo choice rather than a mistake.
"""

from __future__ import annotations

import re
import urllib.parse

# A host, and nothing smuggled alongside it. `evil.com/` and `a@evil.com` both
# fail here, which is the point: these values get interpolated into URLs.
_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?")


def http_url(raw: str, what: str = "URL") -> str:
    """Return ``raw`` unchanged if it is an http(s) URL with a host, else raise."""
    parsed = urllib.parse.urlsplit((raw or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{raw!r} is not an http(s) {what}")
    return parsed.geturl()


def hostname(raw: str, what: str = "host") -> str:
    """Return ``raw`` unchanged if it is a bare host name, else raise."""
    if not _HOSTNAME.fullmatch((raw or "").strip()):
        raise ValueError(f"{raw!r} is not a {what} name")
    return raw.strip()
