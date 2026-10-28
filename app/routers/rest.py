"""Generic REST endpoints: device inventory + instant random measurements.

This is the simplest integration surface for a LIMS: POST a sample id to
/devices/{id}/measure and receive a randomly generated measurement as JSON.
"""

from __future__ import annotations

import random

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import DEVICES, DEVICES_BY_ID, Device, get_device
from ..protocols import labx as labx_mod
from ..simulator import generate_measurement, random_sample_id, now_ts, _rng

router = APIRouter(prefix="/api", tags=["core"])


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

def device_summary(d: Device) -> dict:
    return {
        "device_id": d.device_id,
        "type": d.type,
        "name": d.name,
        "manufacturer": d.manufacturer,
        "model": d.model,
        "serial": d.serial,
        "location": d.location,
        "protocols": d.protocols,
        "tcp_enabled": d.tcp_enabled,
        "default_panel": d.panel,
    }


@router.get("/health", summary="Liveness probe")
def health():
    return {"status": "ok", "time": now_ts("%Y-%m-%dT%H:%M:%S"),
            "devices": len(DEVICES)}


@router.get("/devices", summary="List all simulated instruments")
def list_devices():
    return {"devices": [device_summary(d) for d in DEVICES]}


@router.get("/devices/{device_id}", summary="Show one instrument")
def get_device_info(device_id: str):
    d = get_device(device_id)
    if not d:
        raise HTTPException(404, f"Unknown device '{device_id}'. See GET /api/devices")
    return device_summary(d)


# ---------------------------------------------------------------------------
# Measurements (instant synchronous simulation)
# ---------------------------------------------------------------------------

class MeasureRequest(BaseModel):
    sample_id: str | None = Field(
        default=None,
        description="Barcode / sample id the LIMS assigns. Generated when omitted.",
        examples=["S-2026-48211"],
    )


@router.post("/devices/{device_id}/measure",
             summary="Generate one random measurement for a sample on this instrument",
             description=(
                 "Instant synchronous simulation: returns a complete result set "
                 "(values, units, reference ranges, H/L flags and protocol-specific "
                 "extras such as chromatography peaks or weighing stability)."
             ))
def measure(device_id: str, body: MeasureRequest | None = None):
    d = get_device(device_id)
    if not d:
        raise HTTPException(404, f"Unknown device '{device_id}'. See GET /api/devices")
    sample_id = (body.sample_id if body and body.sample_id else None) or random_sample_id()
    m = generate_measurement(d, sample_id)
    return {
        "device": device_summary(d),
        "measurement": m,
    }


@router.get("/worklist/{device_id}",
            summary="Simulated worklist (orders waiting on the instrument)")
def worklist(device_id: str, limit: int = 5):
    d = get_device(device_id)
    if not d:
        raise HTTPException(404, f"Unknown device '{device_id}'.")
    orders = []
    for _ in range(max(1, min(limit, 20))):
        orders.append({
            "order_id": f"WRK-{_rng.randint(10000, 99999)}",
            "sample_id": random_sample_id(),
            "panel": d.panel,
            "priority": _rng.choice(["ROUTINE", "ROUTINE", "STAT"]),
            "created_at": now_ts("%Y-%m-%dT%H:%M:%S"),
        })
    return {"device_id": d.device_id, "orders": orders}
