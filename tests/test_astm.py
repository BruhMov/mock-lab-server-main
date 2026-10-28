"""ASTM E1394 records and E1381 framing."""

import pytest

from app.config import get_device
from app.protocols import astm as A
from app.simulator import generate_measurement


def fields(record: str) -> dict[int, str]:
    """E1394 field number (1 = record type) -> value."""
    return dict(enumerate(record.split("|"), start=1))


def records(device_id: str, sample_id: str = "S-77") -> tuple[list[str], dict]:
    d = get_device(device_id)
    m = generate_measurement(d, sample_id)
    return A.build_astm_records(d, m), m


# --------------------------------------------------------------- framing

def test_frame_layout():
    frame = A.make_frame(1, b"L|1|N\r")
    body = b"1L|1|N\r\x03"
    assert frame == b"\x02" + body + A.checksum(body).encode() + b"\r\n"


def test_checksum_is_modulo_256():
    assert A.checksum(b"\x80") == "80"
    assert A.checksum(bytes([0xFF, 0x02])) == "01"


def test_frame_numbers_wrap_modulo_8():
    frames = A.message_to_frames([f"R|{i}" for i in range(1, 12)])
    assert [chr(f[1]) for f in frames] == list("12345670123")


def test_long_record_continues_in_etb_frames():
    rec = "C|1|I|" + "X" * 500
    frames = A.message_to_frames([rec])
    assert [f[-5] for f in frames] == [A.ETB, A.ETB, A.ETX]
    assert max(len(f) for f in frames) == 247
    assert b"".join(A.parse_frame(f)[1] for f in frames) == rec.encode() + b"\r"


def test_parse_frame_roundtrip():
    assert A.parse_frame(A.make_frame(3, b"H|\\^&\r")) == (3, b"H|\\^&\r", True)
    assert A.parse_frame(A.make_frame(4, b"R|1|", final=False)) == (4, b"R|1|", False)


@pytest.mark.parametrize("frame", [
    A.make_frame(1, b"H|\\^&\r")[:-4] + b"ZZ\r\n",   # wrong checksum
    b"\x021H|\\^&\r" + b"3C\r\n",                     # no ETX (the pre-fix layout)
    b"\x02XH|\\^&\r\x03" + b"00\r\n",                 # frame number is not a digit
])
def test_parse_frame_rejects_bad_frames(frame):
    with pytest.raises(ValueError):
        A.parse_frame(frame)


# --------------------------------------------------------------- records

def test_header_fields():
    recs, _ = records("ANA-001")
    h = fields(recs[0])
    assert h[2] == "\\^&"
    assert h[5].split("^")[0] == "ANA-001"
    assert h[10] == "LIMS"
    assert (h[12], h[13]) == ("P", "E1394-97")
    assert len(h[14]) == 14 and max(h) == 14


def test_order_fields():
    recs, _ = records("ANA-001", "S-ORDER-1")
    o = fields(recs[2])
    assert o[1] == "O" and o[3] == "S-ORDER-1" and o[4] == ""
    assert o[5] == "^^^BIOCHEM" and o[6] == "R"
    assert o[12] == "N" and o[16] == "SERUM" and o[26] == "F"


def test_result_fields():
    recs, m = records("ANA-001")
    r = fields(next(x for x in recs if x.startswith("R|")))
    v = m["values"][0]
    assert r[3] == f"^^^{v['code']}"
    assert r[4] == f"{v['value']:.2f}" and r[5] == v["unit"] and r[6] == v["ref_range"]
    assert r[7] == v["flag"] and r[9] == "F" and r[11] == m["operator"]
    assert recs[-1] == "L|1|N"


def test_units_with_component_delimiter_are_escaped():
    recs, _ = records("ANA-002")
    wbc = fields(next(r for r in recs if r.startswith("R|") and "^^^WBC" in r))
    assert wbc[5] == "10&S&9/L"


def test_escape():
    assert A.escape("a|b\\c^d&e") == "a&F&b&R&c&S&d&E&e"


def test_only_standard_record_types():
    recs, _ = records("CHR-001")
    types = [r.split("|")[0] for r in recs]
    assert "M" in types  # chromatography peaks
    assert set(types) <= {"H", "P", "O", "R", "C", "M", "L"}


# --------------------------------------------------------------- host query

def test_host_query_prefers_specimen_id_and_reads_receiver():
    msg = (b"H|\\^&|||LIMS|||||ANA-002||P|E1394-97\r"
           b"Q|1|PAT-9^S-2026-9001||^^^ALL||||||||O\r"
           b"L|1|N\r")
    assert A.parse_host_query(msg) == {
        "sample_id": "S-2026-9001", "panel": "ALL", "receiver_id": "ANA-002"}


@pytest.mark.parametrize("q3,expected", [
    ("^S-1", "S-1"), ("S-ONLY", "S-ONLY"), ("ALL", None), ("", None),
])
def test_host_query_sample_fallbacks(q3, expected):
    assert A.parse_host_query(f"Q|1|{q3}||^^^ALL\r".encode())["sample_id"] == expected
