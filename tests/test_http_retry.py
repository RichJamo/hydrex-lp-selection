"""Tests for weekly_bootstrap_update.get_with_retry.

Invariants under test:
  - A retryable status (429/5xx) is retried; a later success is returned.
  - Retries stop after MAX_HTTP_ATTEMPTS and the last error is raised.
  - A non-retryable error status (e.g. 404) raises immediately, no retry.
  - A numeric Retry-After is honoured, capped at RETRY_MAX_DELAY_SECONDS.

Run: python -m unittest discover tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import weekly_bootstrap_update as wbu  # noqa: E402

URL = "https://example.invalid/stats/53"


def _response(status_code, retry_after=None):
    r = requests.Response()
    r.status_code = status_code
    r.url = URL
    r._content = b"{}"
    if retry_after is not None:
        r.headers["Retry-After"] = retry_after
    return r


class GetWithRetryTest(unittest.TestCase):
    def _call(self, responses):
        with mock.patch.object(wbu.requests, "get", side_effect=responses) as get, \
             mock.patch.object(wbu.time, "sleep") as sleep:
            try:
                return wbu.get_with_retry(URL, timeout=5), get, sleep
            except requests.HTTPError as e:
                return e, get, sleep

    def test_retries_429_then_returns_success(self):
        result, get, sleep = self._call([_response(429), _response(503), _response(200)])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(get.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],
                         [wbu.RETRY_BASE_DELAY_SECONDS, wbu.RETRY_BASE_DELAY_SECONDS * 2])

    def test_gives_up_after_max_attempts_and_raises(self):
        responses = [_response(429) for _ in range(wbu.MAX_HTTP_ATTEMPTS)]
        result, get, sleep = self._call(responses)
        self.assertIsInstance(result, requests.HTTPError)
        self.assertEqual(result.response.status_code, 429)
        self.assertEqual(get.call_count, wbu.MAX_HTTP_ATTEMPTS)
        self.assertEqual(sleep.call_count, wbu.MAX_HTTP_ATTEMPTS - 1)

    def test_non_retryable_status_raises_immediately(self):
        result, get, sleep = self._call([_response(404), _response(200)])
        self.assertIsInstance(result, requests.HTTPError)
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_honours_retry_after_with_cap(self):
        result, _, sleep = self._call([_response(429, "7"), _response(429, "600"), _response(200)])
        self.assertEqual(result.status_code, 200)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],
                         [7.0, wbu.RETRY_MAX_DELAY_SECONDS])


if __name__ == "__main__":
    unittest.main()
