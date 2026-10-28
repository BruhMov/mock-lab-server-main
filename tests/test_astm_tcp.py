"""ASTM TCP listener sessions, host side, against 127.0.0.1."""

import asyncio

from app.protocols import astm as A
from app.tcp import astm_tcp

ACK, NAK = bytes([A.ACK]), bytes([A.NAK])
QUERY = [
    "H|\\^&|||LIMS|||||ANA-002||P|E1394-97",
    "Q|1|^S-WANTED-01||^^^ALL||||||||O",
    "L|1|N",
]


async def _session(query: list[str], corrupt_first: bool = False,
                   nak_first_result: bool = False) -> tuple[list[bytes], list[str], int]:
    """Run one host session; returns (replies to query frames, result records, result frames)."""
    server = await asyncio.start_server(astm_tcp.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(bytes([A.ENQ]))
        assert await reader.readexactly(1) == ACK
        replies = []
        for i, fr in enumerate(A.message_to_frames(query)):
            if corrupt_first and i == 0:
                writer.write(fr[:-4] + b"ZZ\r\n")
                replies.append(await reader.readexactly(1))
            writer.write(fr)
            replies.append(await reader.readexactly(1))
        writer.write(bytes([A.EOT]))

        assert await reader.readexactly(1) == bytes([A.ENQ])
        writer.write(ACK)
        text, received, naked = b"", 0, False
        while True:
            first = await reader.readexactly(1)
            if first[0] == A.EOT:
                break
            frame = first + await reader.readuntil(b"\n")
            received += 1
            if nak_first_result and not naked:
                naked = True
                writer.write(NAK)
                continue
            text += A.parse_frame(frame)[1]
            writer.write(ACK)
        return replies, text.decode().split("\r")[:-1], received
    finally:
        writer.close()
        server.close()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


def test_answers_for_queried_device_and_sample():
    replies, recs, _ = run(_session(QUERY))
    assert replies == [ACK] * 3
    assert recs[0].split("|")[4].startswith("ANA-002^")
    order = next(r for r in recs if r.startswith("O|"))
    assert order.split("|")[2] == "S-WANTED-01"
    assert recs[-1] == "L|1|N"


def test_unknown_receiver_falls_back_to_default_device():
    _, recs, _ = run(_session(["H|\\^&", "Q|1|^S-1", "L|1|N"]))
    assert recs[0].split("|")[4].startswith("BAL-001^")


def test_bad_checksum_is_answered_with_nak():
    replies, recs, _ = run(_session(QUERY, corrupt_first=True))
    assert replies == [NAK, ACK, ACK, ACK]
    assert next(r for r in recs if r.startswith("O|")).split("|")[2] == "S-WANTED-01"


def test_device_resends_frame_after_nak():
    _, recs, received = run(_session(QUERY, nak_first_result=True))
    assert received == len(recs) + 1  # the NAKed frame came twice
    assert recs[0].startswith("H|") and recs[-1] == "L|1|N"
