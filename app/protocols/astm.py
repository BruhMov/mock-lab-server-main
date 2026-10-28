r"""ASTM E1381/E1394 protocol builder for the mock analyzers.

Produces classic instrument-to-LIS result records (E1394 / CLSI LIS2-A2)::

    H|\^&|||ANA-001^cobas c311^Roche|||||LIMS||P|E1394-97|20261027103000
    P|1||S-2026-12345||Doe^John||19800101|U
    O|1|S-2026-12345||^^^BIOCHEM|R|20261027103000|||||N||||SERUM||||||||||F
    R|1|^^^GLU|5.40|mmol/L|3.9-6.4|||F||ivanov_i||20261027103000|cobas c311
    L|1|N

and the E1381 low-level framing helpers (ENQ/ACK/EOT, STX frames with
checksums) used by the raw TCP listener in app/tcp/astm_tcp.py.
"""

from __future__ import annotations

from ..config import Device
from ..simulator import now_ts

# Control characters (E1381)
ENQ = 0x05
ACK = 0x06
NAK = 0x15
EOT = 0x04
STX = 0x02
ETB = 0x17
ETX = 0x03
CR = b"\x0d"
LF = b"\x0a"

MAX_FRAME_TEXT = 240  # E1381: at most 240 text characters per frame (247 with framing)


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------

def escape(value) -> str:
    """Escape E1394 delimiters inside a field value (escape delimiter is '&')."""
    s = "" if value is None else str(value)
    return (s.replace("&", "&E&").replace("|", "&F&")
             .replace("\\", "&R&").replace("^", "&S&"))


def _record(rtype: str, fields: dict[int, str]) -> str:
    """Build a record from E1394 field numbers (field 1 is the record type)."""
    out = [rtype] + [""] * (max(fields) - 1)
    for pos, value in fields.items():
        out[pos - 1] = value
    return "|".join(out)


def _num(v, decimals):
    if v is None:
        return ""
    return f"{round(float(v), decimals):.{decimals}f}" if decimals else str(v)


def build_astm_records(device: Device, m: dict) -> list[str]:
    """Build the ordered list of E1394 records (H, P, O, R..., M..., C, L) as strings."""
    ts = now_ts()
    measured = m["measured_at"].replace("-", "").replace(":", "").replace("T", "")
    extra = m.get("extra") or {}

    recs: list[str] = [
        _record("H", {2: "\\^&",
                      5: f"{escape(device.device_id)}^{escape(device.model)}^{escape(device.manufacturer)}",
                      10: "LIMS", 12: "P", 13: "E1394-97", 14: ts}),
        _record("P", {2: "1", 4: escape(m["sample_id"]), 6: "Doe^John", 8: "19800101", 9: "U"}),
        _record("O", {2: "1", 3: escape(m["sample_id"]), 5: f"^^^{escape(m['panel'])}", 6: "R",
                      7: measured, 12: "N", 16: escape(extra.get("sample_type")), 26: "F"}),
    ]

    for i, v in enumerate(m["values"], start=1):
        recs.append(_record("R", {
            2: str(i), 3: f"^^^{escape(v['code'])}",
            4: _num(v["value"], v.get("decimals", 2)), 5: escape(v["unit"]),
            6: escape(v.get("ref_range")), 7: v.get("flag") or "", 9: "F",
            11: escape(m["operator"]), 13: measured, 14: escape(device.model),
        }))

    # Instrument-specific data goes into manufacturer (M) records
    for seq, p in enumerate(extra.get("peaks") or [], start=1):
        recs.append("|".join([
            "M", str(seq), "PEAK", str(p["peak_no"]), escape(p["compound"]), str(p["rt_min"]),
            str(p["area"]), str(p["area_percent"]), str(p["height"]), str(p["resolution"] or ""),
            str(p["tailing_factor"]), str(p["plates"]),
        ]))
    comment = extra.get("stability")
    if comment:
        recs.append(f"C|1|I|STABILITY: {escape(comment)}")

    recs.append("L|1|N")
    return recs


def records_to_message(recs: list[str], line_sep: str = "\r") -> str:
    return line_sep.join(recs)


# ---------------------------------------------------------------------------
# E1381 low-level framing (used by TCP listener / TCP push client)
# ---------------------------------------------------------------------------

def checksum(data: bytes) -> str:
    """E1381 checksum of the bytes from FN through ETB/ETX inclusive, modulo 256."""
    return format(sum(data) & 0xFF, "02X")


def make_frame(frame_number: int, text: bytes, final: bool = True) -> bytes:
    """Wrap message text into an E1381 frame: <STX>FN<text><ETX|ETB><C1C2><CR><LF>.

    FN is sent modulo 8; `final=False` marks an intermediate (ETB) frame.
    """
    body = str(frame_number % 8).encode() + text + bytes([ETX if final else ETB])
    return bytes([STX]) + body + checksum(body).encode() + CR + LF


def message_to_frames(recs: list[str]) -> list[bytes]:
    """One record per frame; a record longer than 240 characters continues in
    intermediate (ETB) frames. Frame numbers run 1..7, 0, 1... across the message."""
    frames: list[bytes] = []
    for rec in recs:
        data = rec.encode("ascii", errors="replace") + CR
        chunks = [data[i:i + MAX_FRAME_TEXT] for i in range(0, len(data), MAX_FRAME_TEXT)]
        for j, chunk in enumerate(chunks):
            frames.append(make_frame(len(frames) + 1, chunk, final=j == len(chunks) - 1))
    return frames


def parse_frame(frame: bytes) -> tuple[int, bytes, bool]:
    """Validate one complete frame (<STX> ... <CR><LF>) and return (FN, text, is_final).

    Raises ValueError on a malformed frame or a checksum mismatch.
    """
    if len(frame) < 7 or frame[0] != STX or not frame.endswith(CR + LF):
        raise ValueError("malformed frame")
    body, cks = frame[1:-4], frame[-4:-2].decode("ascii", errors="replace")
    if body[-1] not in (ETX, ETB):
        raise ValueError("frame has no ETX/ETB terminator")
    if not chr(body[0]).isdigit():
        raise ValueError("frame number is not a digit")
    if checksum(body) != cks.upper():
        raise ValueError(f"checksum mismatch: got {cks}, expected {checksum(body)}")
    return int(chr(body[0])), body[1:-1], body[-1] == ETX


def parse_host_query(payload: bytes) -> dict:
    """Extract sample id / panel from the first Q record and the receiver id
    (H.10) from the header of a host message (best effort).

    Q.3 is ``patient ID^specimen ID^...``: the specimen component is preferred,
    the patient component is the fallback.
    """
    sample_id, panel, receiver_id = None, None, None
    text = payload.decode("ascii", errors="replace")
    for rec in text.replace("\n", "\r").split("\r"):
        f = rec.split("|")
        if f[0] == "H" and receiver_id is None and len(f) > 9:
            receiver_id = f[9].split("^")[0].strip() or None
        elif f[0] == "Q":
            if len(f) > 2:
                comps = [c.strip() for c in f[2].split("\\")[0].split("^")]
                sample_id = next((c for c in comps[1:2] + comps[:1] if c and c.upper() != "ALL"), None)
            if len(f) > 4:
                panel = f[4].split("^")[-1] or None
            break
    return {"sample_id": sample_id, "panel": panel, "receiver_id": receiver_id}
