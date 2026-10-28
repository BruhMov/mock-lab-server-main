"""ASTM E1394 over HTTP.

Lets a LIMS exercise the classic analyzer record layout (H/P/O/R/L records)
without opening a raw TCP socket: request an ASTM message for a sample and
receive it as JSON or plain text. Frame-level output (STX/checksum framing
per E1381) is available too. A push helper lets you test the LIMS's own TCP
listener by having the mock device dial out.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Response

from ..config import DEVICES, get_device
from ..protocols import astm as A
from ..simulator import generate_measurement
from ..tcp.astm_tcp import send_frames
from ..tcp.outbound import PushNotAllowed, open_push_connection

logger = logging.getLogger("mock_lab.push")

router = APIRouter(prefix="/astm", tags=["ASTM E1394"])


def _require_astm(device_id: str):
    d = get_device(device_id)
    if not d:
        raise HTTPException(404, f"Unknown device '{device_id}'.")
    if "astm" not in d.protocols:
        raise HTTPException(400, f"Device {d.device_id} ({d.name}) does not support ASTM. "
                                 f"Supported: {', '.join(d.protocols)}")
    return d


def _build_message(device_id: str, sample_id: str | None, framing: str) -> dict:
    d = _require_astm(device_id)
    m = generate_measurement(d, sample_id or None)
    recs = A.build_astm_records(d, m)
    if framing == "e1381":
        payload = "".join(f.decode("ascii", errors="replace") for f in A.message_to_frames(recs))
    else:
        payload = A.records_to_message(recs, "\r")
    return {"device": d, "sample_id": m["sample_id"], "records": recs, "message": payload}


@router.get("/devices", summary="Instruments that speak ASTM")
def astm_devices():
    return {"devices": [{"device_id": d.device_id, "name": d.name}
                        for d in DEVICES if "astm" in d.protocols]}


@router.post("/devices/{device_id}/message",
             summary="Generate an ASTM E1394 result message (H,P,O,R...,L)",
             description="Query params: sample_id (optional), "
                         "framing=records|e1381 (default records).")
def astm_message(device_id: str,
                 sample_id: str | None = Query(default=None),
                 framing: str = Query(default="records", pattern="^(records|e1381)$")):
    out = _build_message(device_id, sample_id, framing)
    return {"device_id": out["device"].device_id, "sample_id": out["sample_id"],
            "records": out["records"], "message": out["message"]}


@router.post("/devices/{device_id}/frames",
             summary="Same message as E1381 wire frames (raw text/plain)",
             description="Returns <STX>FN<record><CR><ETX><CKS><CR><LF> frames concatenated "
                         "(records over 240 characters continue in <ETB> frames), "
                         "exactly as they would travel over a serial/TCP session.")
def astm_frames_raw(device_id: str,
                    sample_id: str | None = Query(default=None)):
    out = _build_message(device_id, sample_id, "e1381")
    return Response(content=out["message"], media_type="text/plain")


@router.post("/devices/{device_id}/push-tcp",
             summary="Dial a remote LIMS TCP listener and push an ASTM session",
             description=(
                 "Emulates the analyzer initiating a session: connects to host:port, "
                 "sends ENQ, waits for ACK, transmits framed records (a NAKed frame is "
                 "resent up to 6 times), sends EOT. "
                 "Use this to test the TCP listener built into your LIMS."
             ))
async def astm_push(device_id: str, host: str, port: int,
                    sample_id: str | None = Query(default=None)):
    d = _require_astm(device_id)
    m = generate_measurement(d, sample_id or None)
    recs = A.build_astm_records(d, m)
    frames = A.message_to_frames(recs)

    log: list[str] = []
    try:
        reader, writer = await open_push_connection(host, port, timeout=5.0)
    except PushNotAllowed as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(502, f"Cannot connect to {host}:{port}: {e}")

    async def read_ctrl() -> int:
        b = await asyncio.wait_for(reader.read(1), timeout=5.0)
        return b[0] if b else -1

    try:
        log.append("-> ENQ")
        writer.write(bytes([A.ENQ]))
        await writer.drain()
        ctrl = await read_ctrl()
        log.append(f"<- {'ACK' if ctrl == A.ACK else hex(ctrl) if ctrl >= 0 else 'EOF'}")
        if ctrl != A.ACK:
            raise HTTPException(502, f"Remote answered {'EOF' if ctrl < 0 else hex(ctrl)} instead of ACK")

        session_ok, frame_log = await send_frames(reader, writer, frames, timeout=5.0)
        log += frame_log

        writer.write(bytes([A.EOT]))
        await writer.drain()
        log.append("-> EOT")
    except asyncio.TimeoutError:
        raise HTTPException(504, "Timeout while waiting for remote ACK")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    status = "session_complete" if session_ok else "session_aborted"
    logger.info("ASTM push %s -> %s:%s: %s (last frame: %s)", d.device_id, host, port, status, log[-2])
    return {"status": status,
            "device_id": d.device_id, "sample_id": m["sample_id"],
            "records_sent": len(recs), "frames_sent": len(frames), "session_log": log}
