"""HL7 v2 MLLP listener (Minimal Lower Layer Protocol, <VT>msg<FS><CR>).

Behavior:
- QBP^Q11 (or any query containing QPD/QRD)  -> ORU^R01 with random results
  for the sample id parsed from the query.
- ORU^R01 or any other message               -> MSA|AA acknowledgment.

The device answering is the one named in MSH-6 / MSH-5 of the incoming
message (device id or model); otherwise the first TCP-enabled HL7 device.
The TCP stream is split on <FS><CR>, so messages may arrive in several TCP
segments or several messages in one segment.

This emulates how a LIMS would query an analyzer over TCP port 2575
(the classic MLLP port).
"""

from __future__ import annotations

import asyncio
import logging

from ..config import DEVICES, HL7_MLLP_HOST, HL7_MLLP_PORT, Device
from ..protocols import hl7 as H
from ..simulator import generate_measurement

MLLP_STREAM_LIMIT = 1024 * 1024  # largest accepted MLLP frame, bytes

logger = logging.getLogger("mock_lab.hl7_mllp")

_messages_served = 0


async def read_mllp_frame(reader: asyncio.StreamReader) -> str:
    """Read one MLLP frame from the stream and return the message inside.

    Raises IncompleteReadError when the peer closes before <FS><CR>.
    """
    data = await reader.readuntil(b"\x1c\x0d")
    start = data.find(b"\x0b")
    payload = data[start + 1:-2] if start >= 0 else data[:-2]
    return payload.decode("utf-8", errors="replace").strip()


def _hl7_tcp_devices() -> list[Device]:
    return [d for d in DEVICES if d.tcp_enabled and "hl7v2" in d.protocols]


def _device_for_message(message: str) -> Device:
    """Device named by MSH-6, then MSH-5 (device id or model); else the first TCP-enabled HL7 device."""
    candidates = _hl7_tcp_devices()
    for n in (6, 5):
        value = H.msh_field(message, n).split("^")[0].strip().upper().replace(" ", "_")
        for d in candidates:
            if value and value in (d.device_id.upper(), d.model.upper().replace(" ", "_")):
                return d
    return candidates[0] if candidates else DEVICES[0]


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    global _messages_served
    peer = writer.get_extra_info("peername")
    logger.info(f"connection from {peer}")
    try:
        while True:
            try:
                msg = await asyncio.wait_for(read_mllp_frame(reader), timeout=60.0)
            except asyncio.IncompleteReadError:
                break  # peer closed the connection
            if not msg:
                continue

            logger.debug("<- %r", msg)
            msh9 = H.msh_field(msg, 9)
            logger.info(f"received {msh9 or 'UNKNOWN'} ({len(msg)} bytes)")
            device = _device_for_message(msg)

            is_query = msh9.startswith("QBP") or any(
                s.startswith(("QPD", "QRD")) for s in msg.split("\r"))
            if is_query:
                sample_id = H.parse_qbp(msg).get("sample_id") or None
                m = generate_measurement(device, sample_id)
                response = H.build_oru_r01(device, m)
                rtype = f"ORU^R01 (device {device.device_id}, sample {m['sample_id']})"
            else:
                response = H.build_ack(msg, "AA", device=device)
                rtype = "MSA|AA ack"

            logger.debug("-> %r", response)
            writer.write(H.mllp_wrap(response))
            await writer.drain()
            _messages_served += 1
            logger.info(f"-> {rtype} (total: {_messages_served})")
    except asyncio.TimeoutError:
        logger.info("idle timeout; closing connection")
    except Exception as e:
        logger.error(f"error: {e!r}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_hl7_listener() -> asyncio.AbstractServer:
    server = await asyncio.start_server(handle_client, HL7_MLLP_HOST, HL7_MLLP_PORT,
                                        limit=MLLP_STREAM_LIMIT)
    ids = ", ".join(d.device_id for d in _hl7_tcp_devices())
    logger.info(f"MLLP listener on tcp://{HL7_MLLP_HOST}:{HL7_MLLP_PORT} "
                f"(devices by MSH-6/MSH-5: {ids})")
    return server
