"""HL7 v2 message building, ACKs and the MLLP listener."""

import asyncio

from app.config import get_device
from app.protocols import hl7 as H
from app.simulator import generate_measurement
from app.tcp import hl7_mllp


def seg(message: str, name: str, n: int = 0) -> list[str]:
    """Fields of the n-th `name` segment; for non-MSH segments index = field number."""
    return [s.split("|") for s in message.split("\r") if s.startswith(name + "|")][n]


def qbp(sample_id: str, device_id: str = "ANA-002", ctrl_id: str = "Q1") -> str:
    return (f"MSH|^~\\&|LIMS|LAB|XN-1000|{device_id}|20261027120000||QBP^Q11|{ctrl_id}|P|2.5.1\r"
            f"QPD|Q11^Get Results^HL7nnn|QT1|{sample_id}\r")


# --------------------------------------------------------------- messages

def test_oru_fields():
    d = get_device("ANA-002")
    m = generate_measurement(d, "S-3")
    msg = H.build_oru_r01(d, m)
    assert H.msh_field(msg, 9) == "ORU^R01^ORU_R01"
    obx = seg(msg, "OBX")
    assert obx[3] == "WBC^White blood cell count^L"
    assert obx[6] == "10\\S\\9/L"
    assert obx[11] == "F" and obx[16] == m["operator"] and obx[18] == "ANA-002"


def test_obx_set_ids_are_sequential_with_peaks():
    d = get_device("ANA-001")
    m = generate_measurement(d, "S-4")
    m["extra"]["peaks"] = [{"peak_no": 1, "compound": "X", "rt_min": 1.0,
                            "area": 10.0, "area_percent": 100.0}]
    ids = [s.split("|")[1] for s in H.build_oru_r01(d, m).split("\r") if s.startswith("OBX|")]
    assert ids == [str(i) for i in range(1, len(m["values"]) + 2)]


def test_escape():
    assert H.escape("a|b^c&d~e\\f") == "a\\F\\b\\S\\c\\T\\d\\R\\e\\E\\f"


def test_ack_mirrors_routing_and_control_id():
    ack = H.build_ack(qbp("S-1", ctrl_id="Q123"), "AA", device=get_device("ANA-002"))
    msh = ack.split("\r")[0].split("|")
    assert msh[2:6] == ["XN-1000", "ANA-002", "LIMS", "LAB"]
    assert H.msh_field(ack, 9) == "ACK^Q11^ACK"
    assert H.parse_ack(ack) == {"code": "AA", "control_id": "Q123"}


# --------------------------------------------------------------- MLLP listener

async def _exchange(chunks: list[bytes], n_responses: int) -> tuple[list[str], bytes]:
    """Send raw chunks; return the expected responses and anything that came after them."""
    server = await asyncio.start_server(hl7_mllp.handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        for c in chunks:
            writer.write(c)
            await writer.drain()
            await asyncio.sleep(0.05)
        responses = [await hl7_mllp.read_mllp_frame(reader) for _ in range(n_responses)]
        try:
            extra = await asyncio.wait_for(reader.read(1), timeout=0.3)
        except asyncio.TimeoutError:
            extra = b""
        return responses, extra
    finally:
        writer.close()
        server.close()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


def test_fragmented_message_gets_one_response():
    framed = H.mllp_wrap(qbp("S-FRAG-01"))
    (resp,), extra = run(_exchange([framed[:20], framed[20:60], framed[60:]], 1))
    assert extra == b""
    assert H.msh_field(resp, 9).startswith("ORU^R01") and "S-FRAG-01" in resp


def test_coalesced_messages_get_one_response_each():
    data = H.mllp_wrap(qbp("S-A", ctrl_id="Q1")) + H.mllp_wrap(qbp("S-B", ctrl_id="Q2"))
    (r1, r2), extra = run(_exchange([data], 2))
    assert extra == b""
    assert seg(r1, "PID")[3].startswith("S-A^") and seg(r2, "PID")[3].startswith("S-B^")


def test_device_is_chosen_by_msh6():
    (r1, r2), _ = run(_exchange([H.mllp_wrap(qbp("S-1", device_id="ANA-001")),
                                 H.mllp_wrap(qbp("S-2", device_id="ANA-002"))], 2))
    assert H.msh_field(r1, 4) == "ANA-001" and H.msh_field(r2, 4) == "ANA-002"


def test_non_query_message_is_acknowledged():
    d = get_device("ANA-001")
    oru = H.build_oru_r01(d, generate_measurement(d, "S-5"))
    (ack,), _ = run(_exchange([H.mllp_wrap(oru)], 1))
    assert H.parse_ack(ack) == {"code": "AA", "control_id": H.msh_field(oru, 10)}
