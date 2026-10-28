"""HL7 v2.x over HTTP.

Provides ORU^R01 result generation, QBP query handling and an MLLP push
helper for testing a LIMS-side MLLP listener. Messages use \r segment
separators (the real wire format); use mllp=true to receive the message
already wrapped in MLLP <VT>...<FS><CR> framing.
"""

from __future__ import annotations

import asyncio
import logging
import random

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..config import DEVICES, get_device
from ..protocols import hl7 as H
from ..simulator import generate_measurement, random_sample_id
from ..tcp.hl7_mllp import MLLP_STREAM_LIMIT, read_mllp_frame
from ..tcp.outbound import PushNotAllowed, open_push_connection

router = APIRouter(prefix="/hl7", tags=["HL7 v2.x"])
logger = logging.getLogger("mock_lab.push")

_rng = random.Random()


def _require_hl7(device_id: str):
    d = get_device(device_id)
    if not d:
        raise HTTPException(404, f"Unknown device '{device_id}'.")
    if "hl7v2" not in d.protocols:
        raise HTTPException(400, f"Device {d.device_id} ({d.name}) does not support HL7v2. "
                                 f"Supported: {', '.join(d.protocols)}")
    return d


@router.get("/devices", summary="Instruments that speak HL7 v2")
def hl7_devices():
    return {"devices": [{"device_id": d.device_id, "name": d.name}
                        for d in DEVICES if "hl7v2" in d.protocols]}


@router.post("/devices/{device_id}/oru",
             summary="Generate an unsolicited ORU^R01 result message",
             description="Returns the HL7 message with \\r segment separators. "
                         "Set mllp=true to wrap it in MLLP framing "
                         "(returned as application/octet-stream).")
def hl7_oru(device_id: str,
            sample_id: str | None = Query(default=None),
            mllp: bool = Query(default=False)):
    d = _require_hl7(device_id)
    m = generate_measurement(d, sample_id or None)
    msg = H.build_oru_r01(d, m)
    if mllp:
        return Response(content=H.mllp_wrap(msg), media_type="application/octet-stream")
    return {"device_id": d.device_id, "sample_id": m["sample_id"],
            "message_type": "ORU^R01", "message": msg}


@router.post("/devices/{device_id}/query",
             summary="Send a QBP^Q11 query, receive ORU^R01 with random results",
             description="Pass an HL7 QBP message in the body (text/plain) or leave "
                         "empty to auto-generate the query. The mock analyzer answers "
                         "with an ORU^R01 carrying randomly generated results for the "
                         "sample id found in the query (when it can parse one).")
async def hl7_query(device_id: str, request: Request,
                    sample_id: str | None = Query(default=None)):
    d = _require_hl7(device_id)

    raw = await request.body()
    body_text = raw.decode("utf-8", errors="replace").strip()

    if body_text.strip():
        parsed = H.parse_qbp(body_text.strip())
        query_sample = parsed.get("sample_id")
    else:
        # auto-generate a minimal QBP^Q11 query
        query_sample = sample_id
        body_text = (f"MSH|^~\\&|LIMS|LAB|{d.model}|{d.device_id}|20261027120000||QBP^Q11|"
                     f"Q{_rng.randint(100000,999999)}|P|2.5.1\r"
                     f"QPD|Q11^Get Results^HL7nnn||{query_sample or random_sample_id()}\r")
        query_sample = H.parse_qbp(body_text).get("sample_id")

    effective_sample = sample_id or query_sample
    m = generate_measurement(d, effective_sample or None)
    oru = H.build_oru_r01(d, m)
    ack = H.build_ack(body_text, device=d) if body_text.strip() else None

    return {"device_id": d.device_id,
            "query_received": body_text.strip() or None,
            "sample_id": m["sample_id"],
            "response_type": "ORU^R01",
            "response": oru,
            "ack_to_query": ack}


@router.post("/devices/{device_id}/push-mllp",
             summary="Dial a remote LIMS MLLP listener and push an ORU^R01",
             description="Connects to host:port, sends the ORU^R01 inside an MLLP "
                         "frame, reads the acknowledgment and reports it. status is "
                         "accepted (MSA-1 AA/CA), rejected (AE/AR/CE/CR) or no_ack.")
async def hl7_push(device_id: str, host: str, port: int,
                   sample_id: str | None = Query(default=None)):
    d = _require_hl7(device_id)
    m = generate_measurement(d, sample_id or None)
    msg = H.build_oru_r01(d, m)
    ctrl_id = H.msh_field(msg, 10)

    try:
        reader, writer = await open_push_connection(host, port, timeout=5.0, limit=MLLP_STREAM_LIMIT)
    except PushNotAllowed as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(502, f"Cannot connect to {host}:{port}: {e}")

    ack = ""
    try:
        writer.write(H.mllp_wrap(msg))
        await writer.drain()
        try:
            ack = await asyncio.wait_for(read_mllp_frame(reader), timeout=5.0)
        except asyncio.IncompleteReadError as e:  # closed before <FS><CR>
            ack = e.partial.decode("utf-8", errors="replace").strip("\x0b\x1c\r\n ")
    except asyncio.TimeoutError:
        raise HTTPException(504, "Timeout waiting for MLLP acknowledgment")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    parsed = H.parse_ack(ack)
    code = parsed["code"]
    status = ("accepted" if code in ("AA", "CA")
              else "rejected" if code in ("AE", "AR", "CE", "CR") else "no_ack")
    logger.info("HL7 push %s -> %s:%s: %s (MSA-1 %s)", d.device_id, host, port, status, code or "-")
    return {"status": status, "device_id": d.device_id, "sample_id": m["sample_id"],
            "message_control_id": ctrl_id, "ack_code": code or None,
            "ack_control_id_matches": parsed["control_id"] == ctrl_id if code else None,
            "message_preview": msg[:400], "remote_ack": ack or "<no ack received>"}
