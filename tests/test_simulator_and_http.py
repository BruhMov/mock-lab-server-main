"""Simulator flags and the HTTP surface (no TCP listeners are started)."""

from fastapi.testclient import TestClient

from app import simulator
from app.config import get_device
from app.main import app
from app.protocols import astm as A


def values(device_id: str, code: str, n: int = 1000) -> list[dict]:
    d = get_device(device_id)
    return [next(v for v in simulator.generate_measurement(d)["values"] if v["code"] == code)
            for _ in range(n)]


def test_assay_is_flagged_out_of_specification():
    simulator._rng.seed(2026)
    vals = values("CHR-001", "ASSAY")
    assert 0.08 < sum(1 for v in vals if v["flag"]) / len(vals) < 0.25
    for v in vals:
        assert v["flag"] == ("L" if v["value"] < 95 else "H" if v["value"] > 105 else "")


def test_absorbance_above_linear_range_is_flagged_high():
    simulator._rng.seed(2026)
    vals = values("SPC-001", "ABS")
    assert any(v["flag"] == "H" for v in vals)
    for v in vals:
        assert v["flag"] == ("H" if v["value"] > 2.0 else "")


def test_frames_endpoint_returns_valid_e1381_frames():
    # TestClient outside a `with` block does not run the lifespan, so no listeners start
    resp = TestClient(app).post("/astm/devices/ANA-001/frames", params={"sample_id": "S-HTTP-1"})
    assert resp.status_code == 200
    frames = [b"\x02" + f for f in resp.content.split(b"\x02")[1:]]
    text = b"".join(A.parse_frame(f)[1] for f in frames).decode()
    assert text.startswith("H|") and "|S-HTTP-1|" in text
