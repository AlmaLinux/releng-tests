"""Tests for the HTTP helper: retry on 5xx, per-session GET cache, USER_AGENT.

No network required — they spin up a local pytest-httpserver. So no
``online`` marker and they can run in any environment (including CI
without internet).
"""
from __future__ import annotations

from werkzeug.wrappers import Response

from post_check.helpers import http


def test_http_helper_retries_on_502_three_times(httpserver, tmp_path):
    """First two attempts -> 502, third -> 200. The retry strategy should reach 200."""
    httpserver.expect_ordered_request("/foo").respond_with_data("err1", status=502)
    httpserver.expect_ordered_request("/foo").respond_with_data("err2", status=502)
    httpserver.expect_ordered_request("/foo").respond_with_data("ok", status=200)

    s = http.session(cache_path=str(tmp_path / "cache"))
    r = s.get(httpserver.url_for("/foo"))
    assert r.status_code == 200
    assert r.text == "ok"


def test_http_helper_caches_get_within_session(httpserver, tmp_path):
    """The second GET to the same URL does not reach the server — it's served from cache."""
    call_count = {"n": 0}

    def handler(_request):
        call_count["n"] += 1
        return Response(f"call-{call_count['n']}", status=200)

    httpserver.expect_request("/cached").respond_with_handler(handler)

    s = http.session(cache_path=str(tmp_path / "cache"))
    r1 = s.get(httpserver.url_for("/cached"))
    r2 = s.get(httpserver.url_for("/cached"))

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.text == "call-1"
    assert r2.text == "call-1"  # from cache, not call-2
    assert call_count["n"] == 1, "cache should have intercepted the second request"


def test_http_helper_uses_user_agent_post_check(tmp_path):
    """User-Agent must identify post-check — this is visible in mirror logs."""
    s = http.session(cache_path=str(tmp_path / "cache"))
    assert "post-check" in s.headers["User-Agent"]
