"""LabX-style REST API payloads.

Mimics the shape of a metrology/quality data system (e.g. METTLER TOLEDO
LabX): instruments with methods, orders with executions, and results that
include raw measurement metadata such as weighing stability or instrument
calibration state. JSON is the default; GET endpoints can return XML when the
client sends Accept: application/xml.
"""

from __future__ import annotations

import random
from xml.sax.saxutils import escape

from ..config import Device
from ..simulator import now_ts

_rng = random.Random()

# In-memory stores shared by the LabX router
LABX_METHODS: dict[str, list[dict]] = {}
LABX_ORDERS: dict[str, dict] = {}

METHOD_TEMPLATES = {
    "balance": ["WEIGH-BY-DIFF", "TARE-CHECK", "MOISTURE-WEIGH"],
    "chromatograph_gc": ["GC-PURITY-01", "GC-ASSAY-USP"],
    "chromatograph_hplc": ["HPLC-ASSAY-042", "HPLC-IMPURITY-07"],
    "analyzer_chem": ["BIOCHEM-PANEL-12", "GLU-STAT"],
    "analyzer_hema": ["HEMOGRAM-CBC", "RETIC-CHECK"],
    "ph_meter": ["PH-TEST-2.2.43", "PH-SCREEN"],
    "spectrophotometer": ["UV-SCAN-280", "ABS-QUANT"],
}


def instrument_to_labx(device: Device) -> dict:
    return {
        "id": device.device_id,
        "name": device.name,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "serialNumber": device.serial,
        "location": device.location,
        "status": _rng.choice(["IDLE", "IDLE", "BUSY", "IN_MAINTENANCE"]),
        "lastCalibration": now_ts("%Y-%m-%d"),
        "protocols": device.protocols,
    }


def method_to_labx(device_id: str, method: dict) -> dict:
    return {
        "id": method["id"],
        "instrumentId": device_id,
        "name": method["name"],
        "version": method["version"],
        "status": "RELEASED",
        "parameters": method["parameters"],
    }


def seed_methods() -> None:
    from ..config import DEVICES
    for d in DEVICES:
        names = METHOD_TEMPLATES.get(d.type, ["GENERIC-METHOD"])
        LABX_METHODS[d.device_id] = [
            {
                "id": f"M-{d.device_id}-{i + 1:02d}",
                "name": name,
                "version": _rng.randint(1, 5),
                "parameters": {
                    "maxRuntimeMin": _rng.randint(2, 30),
                    "tolerancePercent": round(_rng.uniform(0.5, 2.0), 2),
                },
            }
            for i, name in enumerate(names)
        ]


def seed_orders(n: int = 8) -> None:
    from ..config import DEVICES
    for i in range(n):
        dev = DEVICES[i % len(DEVICES)]
        method = _rng.choice(LABX_METHODS.get(dev.device_id, [{"id": "M-GEN", "name": "GENERIC"}]))
        order_id = f"ORD-{5000 + i}"
        LABX_ORDERS[order_id] = {
            "id": order_id,
            "instrumentId": dev.device_id,
            "methodId": method["id"],
            "methodName": method["name"],
            "sampleId": f"S-2026-{30000 + i * 211}",
            "status": _rng.choice(["NEW", "NEW", "IN_PROGRESS", "COMPLETED"]),
            "priority": _rng.choice(["NORMAL", "NORMAL", "HIGH"]),
            "createdAt": now_ts("%Y-%m-%dT%H:%M:%S"),
            "createdBy": _rng.choice(["labx.admin", "operator1", "qc.bot"]),
            "results": None,
        }


def order_to_labx(order: dict) -> dict:
    out = {k: v for k, v in order.items() if k != "results"}
    out["hasResults"] = order.get("results") is not None
    return out


def results_to_labx(order: dict, m: dict) -> dict:
    return {
        "orderId": order["id"],
        "sampleId": m["sample_id"],
        "instrumentId": order["instrumentId"],
        "methodName": order["methodName"],
        "executedAt": m["measured_at"],
        "operator": m["operator"],
        "state": "APPROVED" if _rng.random() < 0.8 else "NEEDS_REVIEW",
        "measurement": m,
    }


# ---------------------------------------------------------------------------
# Minimal dict -> XML serializer (for Accept: application/xml responses)
# ---------------------------------------------------------------------------

def _serialize(obj, list_item_tag: str = "item") -> str:
    if isinstance(obj, dict):
        return "".join(
            f"<{str(k).replace(' ', '_')}>{_serialize(v, list_item_tag)}</{str(k).replace(' ', '_')}>"
            for k, v in obj.items()
        )
    if isinstance(obj, (list, tuple)):
        return "".join(f"<{list_item_tag}>{_serialize(i, list_item_tag)}</{list_item_tag}>" for i in obj)
    return escape(str(obj)) if obj is not None else ""


def dict_to_xml(tag: str, obj, list_item_tag: str = "item") -> str:
    return f"<{tag}>{_serialize(obj, list_item_tag)}</{tag}>"
