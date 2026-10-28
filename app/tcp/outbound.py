"""Outbound TCP connections of the push helpers, limited by MOCK_PUSH_ALLOWED_HOSTS.

The host is resolved once and the connection goes to an address that passed
the check, so a DNS answer cannot change between the check and the connect.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

from .. import config

log = logging.getLogger("mock_lab.push")


class PushNotAllowed(Exception):
    pass


async def open_push_connection(host: str, port: int, timeout: float, limit: int = 2 ** 16
                               ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to host:port if the allowlist permits it; raises PushNotAllowed otherwise."""
    allow = config.PUSH_ALLOWED_HOSTS
    if allow is None:
        return await asyncio.wait_for(asyncio.open_connection(host, port, limit=limit), timeout)

    names, networks = allow
    infos = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout)
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if host.lower() in names or any(ip in net for net in networks):
            return await asyncio.wait_for(asyncio.open_connection(addr, port, limit=limit), timeout)
    log.warning("push to %s:%s refused: not in MOCK_PUSH_ALLOWED_HOSTS", host, port)
    raise PushNotAllowed(f"{host} ({', '.join(addresses)}) is not in MOCK_PUSH_ALLOWED_HOSTS")
