"""Keep one AIRE running at a time.

Two copies fight over things that cannot be shared: the push-to-talk binding
fires both, so a question is asked and billed twice; the second one cannot
bind the web port; and whichever saves settings last wins, silently undoing
the other. None of that announces itself — you just get odd behaviour.

The lock is the running web server rather than a lock file. A lock file left
behind by a crash blocks the next launch until someone deletes it, whereas a
port either answers or it does not, and a stale one is impossible.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional

import httpx

logger = logging.getLogger("race-engineer")

_PROBE_TIMEOUT = 1.5


def find_running(host: str, port: int) -> bool:
    """Whether an AIRE is already serving on this address.

    Only answers True for something that identifies itself as AIRE, so an
    unrelated service squatting the port is reported as a port clash rather
    than mistaken for our own app.
    """
    if not _port_in_use(host, port):
        return False
    try:
        resp = httpx.get(f"http://{host}:{port}/api/status", timeout=_PROBE_TIMEOUT)
        resp.raise_for_status()
        status = resp.json()
        return isinstance(status, dict) and "telemetry_source" in status
    except Exception:
        # Something else holds the port; let the server report the clash.
        return False


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PROBE_TIMEOUT)
        return sock.connect_ex((host, port)) == 0


def show_existing(host: str, port: int) -> bool:
    """Ask the running copy to bring its window up.

    Launching the app again is a request to see it, not to start a second
    one — most often after it has been hidden to the tray and forgotten.
    """
    try:
        resp = httpx.post(f"http://{host}:{port}/api/window/show", timeout=_PROBE_TIMEOUT)
        return resp.status_code == 200
    except Exception as exc:
        logger.debug("Could not raise the existing window: %s", exc)
        return False
