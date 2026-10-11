from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "Mozilla/5.0 (compatible; PolycastPaperResearch/0.1; +https://docs.polymarket.com/)"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def fetch_bytes(url: str, *, accept: str = "application/json", timeout: float = 15,
                user_agent: str = USER_AGENT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {url}: {exc.reason}") from exc
    except OSError as exc:
        # A timeout or reset while reading an already-open response need not
        # be wrapped in URLError. Keep the same failure type callers isolate.
        raise RuntimeError(f"Network I/O failed from {url}: {type(exc).__name__}") from exc
    if len(data) > MAX_RESPONSE_BYTES:
        raise RuntimeError(f"Response too large from {url}")
    return data


def fetch_json(url: str, *, user_agent: str = USER_AGENT) -> Any:
    return json.loads(fetch_bytes(url, user_agent=user_agent))

