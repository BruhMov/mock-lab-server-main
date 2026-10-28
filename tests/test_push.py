"""push-tcp / push-mllp helpers against fake LIMS listeners on 127.0.0.1."""

import asyncio
from functools import partial

import pytest

from app.protocols import astm as A
from app.protocols import hl7 as H
from app.routers import astm as astm_router
from app.routers import hl7 as hl7_router
from app.tcp.hl7_mllp import read_mllp_frame


async def fake_astm_lims(reader, writer, naks: int):
    """Receive one ASTM session; NAK the first `naks` transmissions of frame 1."""
    assert (await reader.readexactly(1))[0] == A.ENQ
    writer.write(bytes([A.ACK]))
    while True:
        first = await reader.readexactly(1)
        if first[0] == A.EOT:
            break
        fn, _, _ = A.parse_frame(first + await reader.readuntil(b"\n"))
        if fn == 1 and naks > 0:
            naks -= 1
            writer.write(bytes([A.NAK]))
        else:
            writer.write(bytes([A.ACK]))
        await writer.drain()
    writer.close()


async def fake_mllp_lims(reader, writer, code: str | None):
    """Receive one message; answer with an ACK carrying `code`, or close without one."""
    msg = await read_mllp_frame(reader)
    if code:
        writer.write(H.mllp_wrap(H.build_ack(msg, code)))
        await writer.drain()
    writer.close()


async def _push(handler, push):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        return await push("127.0.0.1", port)
    finally:
        server.close()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


def astm_push(host, port):
    return astm_router.astm_push("ANA-001", host, port, sample_id="S-PUSH-1")


def hl7_push(host, port):
    return hl7_router.hl7_push("ANA-002", host, port, sample_id="S-PUSH-2")


def test_push_tcp_resends_naked_frame():
    out = run(_push(partial(fake_astm_lims, naks=2), astm_push))
    assert out["status"] == "session_complete"
    log = out["session_log"]
    assert log[2:5] == ["frame 1 -> NAK (attempt 1)", "frame 1 -> NAK (attempt 2)",
                        "frame 1 -> ACK (attempt 3)"]
    assert log[-1] == "-> EOT"


def test_push_tcp_aborts_after_six_naks():
    out = run(_push(partial(fake_astm_lims, naks=99), astm_push))
    assert out["status"] == "session_aborted"
    assert sum("frame 1 -> NAK" in line for line in out["session_log"]) == 6


@pytest.mark.parametrize("code,status", [
    ("AA", "accepted"), ("AE", "rejected"), ("AR", "rejected"),
])
def test_push_mllp_reports_ack_code(code, status):
    out = run(_push(partial(fake_mllp_lims, code=code), hl7_push))
    assert out["status"] == status
    assert out["ack_code"] == code
    assert out["ack_control_id_matches"] is True


def test_push_mllp_without_ack():
    out = run(_push(partial(fake_mllp_lims, code=None), hl7_push))
    assert out["status"] == "no_ack" and out["ack_code"] is None
