"""The outbound-target checks, and the call sites that have to use them.

Every request this repo makes carries a credential — ROCKETRIDE_APIKEY,
SLACK_API_TOKEN — so the target is checked before the credential travels to it.
These tests exist because the interesting regression is not a bad URL slipping
through the checker; it is a call site quietly stopping calling it.
"""

from __future__ import annotations

import unittest

from agent.config import Settings
from agent.net import hostname, http_url
from rocketride.client import RocketRide, RocketRideError

BAD_URLS = ("file:///etc/passwd", "ftp://host/f", "gopher://h", "https:///no-host",
            "api.rocketride.ai/hook", "", "   ")


class HttpUrl(unittest.TestCase):
    def test_http_and_https_pass(self):
        for url in ("https://api.rocketride.ai/v1", "http://localhost:8080/x"):
            self.assertEqual(http_url(url), url)

    def test_every_other_scheme_is_refused(self):
        for bad in BAD_URLS:
            with self.assertRaises(ValueError, msg=bad):
                http_url(bad)

    def test_the_message_names_what_was_being_checked(self):
        """An operator reading this is looking for which env var to fix."""
        with self.assertRaises(ValueError) as caught:
            http_url("ftp://x", "ROCKETRIDE_URI")
        self.assertIn("ROCKETRIDE_URI", str(caught.exception))


class Hostname(unittest.TestCase):
    def test_a_bare_host_passes(self):
        for host in ("127.0.0.1", "localhost", "api.internal", "a-b.example.com"):
            self.assertEqual(hostname(host), host)

    def test_anything_that_could_move_the_origin_is_refused(self):
        """`evil.com/` and `a@evil.com` both redirect an f-string-built URL to
        somewhere else entirely, taking the bearer token with them."""
        for bad in ("evil.com/", "a@evil.com", "h:99", "", "/", "x y"):
            with self.assertRaises(ValueError, msg=bad):
                hostname(bad)


class ClientBaseUrl(unittest.TestCase):
    """RocketRide pins its base URL at construction, not at the first request."""

    @staticmethod
    def _config(uri: str) -> Settings:
        config = Settings()
        object.__setattr__(config, "rocketride_uri", uri)
        object.__setattr__(config, "rocketride_api_key", "rr_test_key")
        return config

    def test_a_good_uri_is_kept_without_its_trailing_slash(self):
        client = RocketRide(self._config("https://api.rocketride.ai/v1/"))
        self.assertEqual(client.base, "https://api.rocketride.ai/v1")

    def test_a_bad_uri_fails_before_any_request_is_built(self):
        for bad in BAD_URLS:
            with self.assertRaises(RocketRideError, msg=bad):
                RocketRide(self._config(bad))


if __name__ == "__main__":
    unittest.main()
