"""HTTP helper: requests session with a retry strategy and cache (requests-cache).

All online tests must go through `session()` only — otherwise we lose retry,
cache, and a unified User-Agent.
"""

from __future__ import annotations

from typing import Optional

import requests
import requests_cache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "post-check/0.1 (+https://github.com/AlmaLinux/post-check)"


def session(*, cache_path: Optional[str] = None) -> requests.Session:
    """Returns requests.Session with retry on 429/5xx + sqlite cache for GET/HEAD for 1 hour."""
    s = requests_cache.CachedSession(
        cache_name=cache_path or ".requests_cache",
        backend="sqlite",
        expire_after=3600,
        allowable_methods=("GET", "HEAD"),
    )
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers["User-Agent"] = USER_AGENT
    return s
