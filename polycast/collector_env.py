"""Environment for the public RTDS Node collector."""

from __future__ import annotations

import os
from urllib.request import getproxies


def node_collector_env() -> dict[str, str]:
    env = os.environ.copy()
    if not any(key.lower() == "https_proxy" for key in env):
        proxy = getproxies().get("https")
        if proxy:
            env["HTTPS_PROXY"] = proxy
    if any(key.lower() == "https_proxy" for key in env):
        env.setdefault("NODE_USE_ENV_PROXY", "1")
        env.setdefault("NODE_USE_SYSTEM_CA", "1")
    return env
