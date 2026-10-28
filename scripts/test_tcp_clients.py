"""Smoke test for the raw TCP listeners of the mock lab server.

1. Acts as a LIMS host against the ASTM TCP listener :5150
   (ENQ -> query frames -> EOT -> ACK device result frames).
2. Acts as a LIMS MLLP client against :2575 (QBP^Q11 -> ORU^R01).

Framing is implemented here independently of the server code, per E1381:
<STX>FN<text><ETX><C1C2><CR><LF>, checksum = sum(FN..ETX) mod 256.
"""
import socket

HOST = "127.0.0.1"


# ---------------------------------------------------------------- ASTM host
def astm_checksum(data: bytes) -> str:
    return format(sum(data) % 256, "02X")


def astm_frame(n: int, text: str) -> bytes:
    body = str(n % 8).encode() + text.encode() + b"\r\x03"
    return b"\x02" + body + astm_checksum(body).encode() + b"\r\n"


def recv_until(s: socket.socket, end: bytes) -> bytes:
    buf = b""
    while not buf.endswith(end):
        chunk = s.recv(1)
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def test_astm():
    print("== ASTM raw TCP test (host side) ==")
    s = socket.create_connection((HOST, 5150), timeout=10)
    s.sendall(b"\x05")                      # ENQ
    assert s.recv(1) == b"\x06", "no ACK for ENQ"
    print("ENQ -> ACK ok")

    query_recs = [
        "H|\\^&|||LIMS^1|||||ANA-001||P|E1394-97|20261027120000",   # H.10 = device asked
        "Q|1|^S-2026-9001||^^^ALL||||||||O",                        # Q.3 = ^specimen id
        "L|1|N",
    ]
    for i, rec in enumerate(query_recs, start=1):
        s.sendall(astm_frame(i, rec))
        assert s.recv(1) == b"\x06", f"no ACK for frame {i}"
    s.sendall(b"\x04")                      # EOT
    print("query frames ACKed, EOT sent")

    # device should now send ENQ to transmit results
    ctrl = s.recv(1)
    assert ctrl == b"\x05", f"expected device ENQ, got {ctrl!r}"
    s.sendall(b"\x06")                      # ACK it

    text = b""
    while True:
        first = s.recv(1)
        if first == b"\x04":                # EOT: transfer finished
            break
        assert first == b"\x02", f"expected STX, got {first!r}"
        frame = recv_until(s, b"\r\n")      # FN text ETX|ETB C1 C2 CR LF
        body, cks = frame[:-4], frame[-4:-2].decode()
        assert body[-1:] in (b"\x03", b"\x17"), "frame without ETX/ETB"
        assert cks == astm_checksum(body), f"bad checksum {cks}"
        text += body[1:-1]
        s.sendall(b"\x06")                  # ACK each frame
    s.close()

    records = text.decode("ascii", errors="replace").split("\r")[:-1]
    print(f"received {len(records)} result records:")
    for r in records:
        print("   ", r)
    assert records and records[0].startswith("H|"), "no H record received"
    assert records[0].split("|")[4].startswith("ANA-001"), "wrong device answered"
    order = next(r for r in records if r.startswith("O|"))
    assert order.split("|")[2] == "S-2026-9001", "queried sample id not echoed in O.3"
    print("ASTM TCP test PASSED\n")


# ---------------------------------------------------------------- HL7 MLLP
def test_mllp():
    print("== HL7 MLLP test ==")
    qbp = (
        "MSH|^~\\&|LIMS|LAB|XN-1000|ANA-002|20261027120000||QBP^Q11|Q123456|P|2.5.1\r"
        "QPD|Q11^Get Results^HL7nnn||S-2026-9002\r"
    )
    s = socket.create_connection((HOST, 2575), timeout=10)
    s.sendall(b"\x0b" + qbp.encode() + b"\x1c\x0d")
    resp = recv_until(s, b"\x1c\x0d").decode("utf-8", errors="replace").strip("\x0b\x1c\r\n ")
    s.close()
    print(resp.replace("\r", "\n"))
    assert resp.startswith("MSH|") and "ORU^R01" in resp and "S-2026-9002" in resp
    assert resp.split("\r")[0].split("|")[3] == "ANA-002", "wrong device answered"
    print("MLLP test PASSED\n")


if __name__ == "__main__":
    test_astm()
    test_mllp()
