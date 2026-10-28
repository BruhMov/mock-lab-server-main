"""LabX-style REST API (metrology/quality data system emulation).

Mirrors the resource layout of systems like METTLER TOLEDO LabX:
instruments -> methods -> orders -> executions -> results. JSON by default,
XML when the client sends Accept: application/xml.
"""

from __future__ import annotations

import random

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ..config import DEVICES, get_device
from ..protocols import labx as L
from ..simulator import generate_measurement, now_ts, _rng

router = APIRouter(prefix="/labx/v1", tags=["LabX emulation"])

L.seed_methods()
L.seed_orders()


def _wants_xml(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/xml" in accept or "text/xml" in accept


def _respond(request: Request, payload: dict, root: str = "labx", status: int = 200) -> Response:
    if _wants_xml(request):
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + L.dict_to_xml(root, payload)
        return Response(content=xml, media_type="application/xml", status_code=status)
    return payload


# ---------------------------------------------------------------------------
# Instruments & methods
# ---------------------------------------------------------------------------

@router.get("/instruments", summary="List registered instruments")
def instruments(request: Request):
    return _respond(request, {"instruments": [L.instrument_to_labx(d) for d in DEVICES]},
                    root="instruments")


@router.get("/instruments/{device_id}", summary="Instrument details")
def instrument(device_id: str, request: Request):
    d = get_device(device_id)
    if not d:
        raise HTTPException(404, f"Instrument '{device_id}' not found")
    return _respond(request, L.instrument_to_labx(d), root="instrument")


@router.get("/instruments/{device_id}/methods", summary="Methods installed on an instrument")
def methods(device_id: str, request: Request):
    d = get_device(device_id)
    if not d:
        raise HTTPException(404, f"Instrument '{device_id}' not found")
    ms = L.LABX_METHODS.get(d.device_id, [])
    return _respond(request, {"methods": [L.method_to_labx(d.device_id, m) for m in ms]},
                    root="methods")


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@router.get("/orders", summary="List orders")
def orders(request: Request, status: str | None = Query(default=None)):
    items = [o for o in L.LABX_ORDERS.values()
             if status is None or o["status"] == status]
    return _respond(request, {"orders": [L.order_to_labx(o) for o in items]}, root="orders")


@router.get("/orders/{order_id}", summary="Order details")
def order(order_id: str, request: Request):
    o = L.LABX_ORDERS.get(order_id)
    if not o:
        raise HTTPException(404, f"Order '{order_id}' not found")
    return _respond(request, L.order_to_labx(o), root="order")


@router.post("/orders", status_code=201,
             summary="Create an order (fields auto-filled when omitted)")
def create_order(request: Request, body: dict | None = None):
    body = body or {}
    device_id = body.get("instrumentId") or body.get("device_id")
    device = None
    if device_id:
        device = get_device(device_id)
        if not device:
            raise HTTPException(404, f"Instrument '{device_id}' not found")
    device = device or DEVICES[_rng.randrange(len(DEVICES))]
    ms = L.LABX_METHODS.get(device.device_id) or [{"id": "M-GEN", "name": "GENERIC"}]
    method = next((m for m in ms if m["id"] == body.get("methodId")), None) or ms[0]

    order_id = f"ORD-{_rng.randint(10000, 99999)}"
    L.LABX_ORDERS[order_id] = {
        "id": order_id,
        "instrumentId": device.device_id,
        "methodId": method["id"],
        "methodName": method["name"],
        "sampleId": body.get("sampleId") or f"S-2026-{_rng.randint(10000, 99999)}",
        "status": "NEW",
        "priority": body.get("priority", "NORMAL"),
        "createdAt": now_ts("%Y-%m-%dT%H:%M:%S"),
        "createdBy": body.get("createdBy", "api.client"),
        "results": None,
    }
    return _respond(request, L.order_to_labx(L.LABX_ORDERS[order_id]), root="order", status=201)


@router.post("/orders/{order_id}/execution",
             summary="Execute an order on its instrument (generates random results)",
             description="Simulates the operator/instrument running the order: "
                         "the status moves to COMPLETED and results become available "
                         "via GET /orders/{id}/results.")
def execute_order(order_id: str, request: Request):
    o = L.LABX_ORDERS.get(order_id)
    if not o:
        raise HTTPException(404, f"Order '{order_id}' not found")
    device = get_device(o["instrumentId"])
    m = generate_measurement(device, o["sampleId"])
    results = L.results_to_labx(o, m)
    o["results"] = results
    o["status"] = "COMPLETED"
    return _respond(request, {"executionId": f"EX-{_rng.randint(10000, 99999)}",
                              "status": "COMPLETED",
                              "resultId": results["orderId"]},
                    root="execution", status=201)


@router.get("/orders/{order_id}/results", summary="Fetch results of a completed order")
def order_results(order_id: str, request: Request):
    o = L.LABX_ORDERS.get(order_id)
    if not o:
        raise HTTPException(404, f"Order '{order_id}' not found")
    if not o.get("results"):
        raise HTTPException(409, f"Order '{order_id}' has no results yet. "
                                 f"POST /labx/v1/orders/{order_id}/execution first.")
    return _respond(request, o["results"], root="results")


@router.post("/results", status_code=201,
             summary="Ad-hoc result submission endpoint (accepts any JSON payload)")
def submit_results(request: Request, body: dict | None = None):
    """Useful for testing the LIMS's inbound result-acceptance endpoint."""
    return _respond(request, {"acknowledged": True,
                              "receivedAt": now_ts("%Y-%m-%dT%H:%M:%S"),
                              "echo": body or {}},
                    root="ack", status=201)
