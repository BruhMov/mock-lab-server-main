"""HL7 v2.5 message builder for the mock analyzers (ORU^R01 unsolicited result,
QBP^Q11 parsing and ACK generation). Messages use \r as segment separator,
matching the wire format carried inside MLLP frames.
"""

from __future__ import annotations

import random

from ..config import Device
from ..simulator import now_ts

_rng = random.Random()

ENCODING_CHARS = "^~\\&"


def _ts() -> str:
    return now_ts()


def escape(value) -> str:
    """Escape HL7 v2 delimiters inside a field value (escape character is '\\')."""
    s = "" if value is None else str(value)
    return (s.replace("\\", "\\E\\").replace("|", "\\F\\").replace("^", "\\S\\")
             .replace("&", "\\T\\").replace("~", "\\R\\"))


def _segment(name: str, fields: dict[int, str]) -> str:
    """Build a segment from HL7 field numbers (not for MSH, whose numbering is shifted)."""
    out = [name] + [""] * max(fields)
    for n, value in fields.items():
        out[n] = value
    return "|".join(out)


def msh_field(message: str, n: int) -> str:
    """Value of MSH-n (n >= 2) of a message, or "" when absent."""
    msh = next((s for s in message.split("\r") if s.startswith("MSH")), None)
    if not msh:
        return ""
    f = msh.split("|")  # MSH-1 is the field separator itself, so MSH-n is f[n - 1]
    return f[n - 1] if len(f) >= n else ""


def build_oru_r01(device: Device, m: dict) -> str:
    """Build a complete ORU^R01 result message from a simulated measurement."""
    sending_app = f"{device.model.replace(' ', '_')}"
    msg_ctrl_id = f"MSG{_rng.randint(100000, 999999)}"
    ts = _ts()
    hl7_ts = m["measured_at"].replace("-", "").replace(":", "").replace("T", "")
    sample, panel = escape(m["sample_id"]), escape(m["panel"])

    segs = [
        f"MSH|^~\\&|{sending_app}|{device.device_id}|LIMS|LAB|{ts}||ORU^R01^ORU_R01|{msg_ctrl_id}|P|2.5.1",
        f"PID|1||{sample}^^^LAB^MR||Doe^John||19800101|U",
        f"OBR|1||{sample}|{panel}^{panel}^L|||{hl7_ts}|||||||||||||||{hl7_ts}|||F",
    ]

    set_id = 0
    for v in m["values"]:
        set_id += 1
        dec = v.get("decimals", 2)
        segs.append(_segment("OBX", {
            1: str(set_id), 2: "NM", 3: f"{escape(v['code'])}^{escape(v['name'])}^L", 4: "1",
            5: f"{round(float(v['value']), dec):.{dec}f}", 6: escape(v["unit"]),
            7: escape(v.get("ref_range")), 8: v.get("flag") or "", 11: "F", 14: hl7_ts,
            16: escape(m["operator"]), 18: escape(device.device_id),
        }))

    for p in (m.get("extra") or {}).get("peaks") or []:
        set_id += 1
        segs.append(_segment("OBX", {
            1: str(set_id), 2: "ST", 3: f"PK{p['peak_no']}^{escape(p['compound'])}^L", 4: "1",
            5: f"RT={p['rt_min']}min;AREA={p['area']};AREA%={p['area_percent']}",
            8: "N", 11: "F", 14: hl7_ts,
        }))

    return "\r".join(segs)


def parse_qbp(message: str) -> dict:
    """Best-effort extraction of sample id from a QBP^Q11 style query."""
    sample_id = None
    for seg in message.split("\r"):
        if not (seg.startswith("QPD") or seg.startswith("QRD")):
            continue
        f = seg.split("|")
        # sample id normally sits in QPD-3..8 (skip QPD-1 query name / QPD-2)
        for cand in f[3:9]:
            for part in cand.replace("^", "|").split("|"):
                part = part.strip()
                if part and not part.startswith("Q1") and "HL7" not in part:
                    sample_id = part
                    break
            if sample_id:
                break
        if sample_id:
            break
    return {"sample_id": sample_id}


def build_ack(message: str, code: str = "AA", device: Device | None = None) -> str:
    """Build an ACK for a received message.

    MSH-5/6 mirror the sender (MSH-3/4) of the message, MSA-2 echoes its
    MSH-10. The ACK is sent by `device` when given, otherwise by LIMS^LAB.
    """
    sender_app = msh_field(message, 3) or "UNKNOWN"
    sender_fac = msh_field(message, 4)
    ctrl_id = msh_field(message, 10) or "UNKNOWN"
    trigger = (msh_field(message, 9).split("^") + [""])[1]
    app, fac = (device.model.replace(" ", "_"), device.device_id) if device else ("LIMS", "LAB")
    msg_type = f"ACK^{trigger}^ACK" if trigger else "ACK"
    return "\r".join([
        f"MSH|^~\\&|{app}|{fac}|{sender_app}|{sender_fac}|{_ts()}||{msg_type}|"
        f"ACK{_rng.randint(100000, 999999)}|P|2.5.1",
        f"MSA|{code}|{ctrl_id}",
    ])


def parse_ack(message: str) -> dict:
    """MSA-1 acknowledgment code and MSA-2 message control id of an ACK."""
    msa = next((s.split("|") for s in message.split("\r") if s.startswith("MSA|")), [])
    return {"code": msa[1] if len(msa) > 1 else "",
            "control_id": msa[2] if len(msa) > 2 else ""}


def mllp_wrap(message: str) -> bytes:
    """Wrap a message into an MLLP frame: <VT> message <FS><CR>"""
    return b"\x0b" + message.encode("utf-8") + b"\x1c\x0d"


def mllp_wrap_batch(messages: list[str]) -> bytes:
    return b"".join(mllp_wrap(m) for m in messages)
