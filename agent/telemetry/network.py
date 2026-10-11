"""This machine's address on its own network, for multi-machine jobs."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import psutil


def local_network_address(toward_url: str) -> tuple[str | None, str | None]:
    """(address, interface name) this machine uses to reach ``toward_url``.

    A UDP "connect" sends nothing; it only asks the OS which local address it
    would route from, which is the one on the real network rather than a
    virtual adapter. The interface name is what gloo needs to bind to it.
    """
    host = urlparse(toward_url).hostname or toward_url
    address = None
    # An orchestrator on this same machine routes over loopback, which no other
    # machine can use; the route toward the internet then names the real one.
    for target in (host, "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 80))
                address = str(probe.getsockname()[0])
        except OSError:
            continue
        if not address.startswith("127."):
            break
        address = None
    if address is None:
        return None, None
    for name, entries in psutil.net_if_addrs().items():
        if any(entry.address == address for entry in entries):
            return address, name
    return address, None
