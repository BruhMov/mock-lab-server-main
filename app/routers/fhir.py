"""FHIR R4 REST endpoints.

A small FHIR server facade over the same random measurement engine: Device
inventory, ServiceRequest orders, generated Observations and DiagnosticReports.
Responses are application/fhir+json, errors are OperationOutcome (see the
exception handlers in app/main.py). Generated Observations and
DiagnosticReports are kept in memory, so references between them resolve.
"""

from __future__ import annotations

import itertools

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..config import DEVICES, Device, get_device
from ..protocols import fhir as F
from ..simulator import generate_measurement, now_ts, _rng


class FHIRResponse(JSONResponse):
    media_type = "application/fhir+json"


router = APIRouter(prefix="/fhir", tags=["FHIR R4"], default_response_class=FHIRResponse)

# In-memory ServiceRequest store, seeded at import time
SERVICE_REQUESTS: dict[str, dict] = {}
for sr in F.seed_service_requests():
    SERVICE_REQUESTS[sr["id"]] = sr
_sr_ids = itertools.count(10000)

# Generated Observations / DiagnosticReports by (resourceType, id); oldest dropped first
RESOURCES: dict[tuple[str, str], dict] = {}
MAX_STORED_RESOURCES = 10_000


def _base(request: Request) -> str:
    return f"{request.base_url}fhir/"


def _store(resources: list[dict]) -> None:
    for r in resources:
        RESOURCES[(r["resourceType"], r["id"])] = r
    while len(RESOURCES) > MAX_STORED_RESOURCES:
        RESOURCES.pop(next(iter(RESOURCES)))


def _read(resource_type: str, resource_id: str) -> dict:
    r = RESOURCES.get((resource_type, resource_id))
    if not r:
        raise HTTPException(404, f"{resource_type}/{resource_id} not found")
    return r


def _created(request: Request, resource: dict) -> FHIRResponse:
    location = f"{_base(request)}{resource['resourceType']}/{resource['id']}"
    return FHIRResponse(resource, status_code=201, headers={"Location": location})


def _fhir_device(device_id: str | None) -> Device:
    """Device that speaks FHIR: the requested one (422 otherwise) or a random one."""
    if not device_id:
        devices = F.fhir_devices()
        if not devices:
            raise HTTPException(422, "No FHIR-capable devices are configured")
        return devices[_rng.randrange(len(devices))]
    d = get_device(device_id)
    if not d:
        raise HTTPException(422, f"Unknown device '{device_id}'")
    if "fhir" not in d.protocols:
        raise HTTPException(422, f"Device {d.device_id} does not support FHIR. "
                                 f"Supported: {', '.join(d.protocols)}")
    return d


def _generate(sample_id: str, device: Device, based_on: str | None = None) -> tuple[dict, list[dict]]:
    """Run the device for a sample; store and return (DiagnosticReport, Observations)."""
    m = generate_measurement(device, sample_id)
    observations = F.measurement_to_observations(device, m, based_on)
    report = F.measurement_to_diagnostic_report(device, m, observations, based_on)
    _store(observations + [report])
    return report, observations


@router.get("/metadata", summary="CapabilityStatement")
def metadata(request: Request):
    return F.capability_statement(_base(request))


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------

@router.get("/Device", summary="Search simulated devices")
def search_devices(request: Request):
    return F.bundle("searchset", _base(request), [F.device_to_fhir(d) for d in DEVICES])


@router.get("/Device/{device_id}", summary="Read one simulated device")
def read_device(device_id: str):
    d = get_device(device_id)
    if not d:
        raise HTTPException(404, f"Unknown device '{device_id}'")
    return F.device_to_fhir(d)


# ---------------------------------------------------------------------------
# ServiceRequest
# ---------------------------------------------------------------------------

@router.get("/ServiceRequest", summary="Search lab orders")
def search_service_requests(request: Request, status: str | None = Query(default=None)):
    items = [s for s in SERVICE_REQUESTS.values()
             if status is None or s["status"] == status]
    return F.bundle("searchset", _base(request), [F.service_request_to_fhir(s) for s in items])


def _performer_device(body: dict) -> str | None:
    """Device id from performer (Device/<id> or identifier) or the target-device extension."""
    refs = list(body.get("performer") or [])
    refs += [e.get("valueReference") or {} for e in body.get("extension") or []
             if str(e.get("url", "")).endswith("/target-device")]
    for ref in refs:
        reference = str(ref.get("reference") or "")
        if reference.startswith("Device/"):
            return reference.split("/", 1)[1]
        value = (ref.get("identifier") or {}).get("value")
        if value and get_device(value):
            return value
    return None


def _service_request_fields(body: dict) -> dict:
    """Sample, device, panel and requester from a FHIR ServiceRequest, or from the
    simplified body {sample_id, device, panel, requester} accepted before."""
    if body.get("resourceType") not in (None, "ServiceRequest"):
        raise HTTPException(400, f"Expected a ServiceRequest, got {body['resourceType']}")
    try:
        specimen = (body.get("specimen") or [{}])[0]
        code = body.get("code") or {}
        requester = body.get("requester")
        return {
            "sample_id": (body.get("sample_id")
                          or (specimen.get("identifier") or {}).get("value")
                          or ((body.get("subject") or {}).get("identifier") or {}).get("value")),
            "device_id": body.get("device") or body.get("device_id") or _performer_device(body),
            "panel": (body.get("panel") or ((code.get("coding") or [{}])[0]).get("code")
                      or code.get("text")),
            "requester": requester.get("display") if isinstance(requester, dict) else requester,
        }
    except (AttributeError, IndexError, TypeError) as e:
        raise HTTPException(400, f"Malformed ServiceRequest: {e}")


@router.post("/ServiceRequest", status_code=201,
             summary="Create a lab order",
             description="Accepts a FHIR ServiceRequest: the sample is taken from "
                         "specimen[0].identifier (or subject.identifier), the device from "
                         "performer (Device/<id>), the panel from code.coding[0].code. "
                         "The simplified body {sample_id, device, panel, requester} still "
                         "works. Missing fields are auto-filled.")
def create_service_request(request: Request, body: dict | None = None):
    fields = _service_request_fields(body or {})
    device = _fhir_device(fields["device_id"])
    sr_id = f"SR-{next(_sr_ids)}"
    sr = {
        "id": sr_id,
        "device_id": device.device_id,
        "sample_id": fields["sample_id"] or f"S-2026-{_rng.randint(10000, 99999)}",
        "panel": fields["panel"] or device.panel,
        "status": "active",
        "authored_on": now_ts("%Y-%m-%dT%H:%M:%S"),
        "requester": fields["requester"] or "LIMS-Integration",
    }
    SERVICE_REQUESTS[sr_id] = sr
    return _created(request, F.service_request_to_fhir(sr))


@router.get("/ServiceRequest/{sr_id}", summary="Read one lab order")
def read_service_request(sr_id: str):
    sr = SERVICE_REQUESTS.get(sr_id)
    if not sr:
        raise HTTPException(404, f"ServiceRequest/{sr_id} not found")
    return F.service_request_to_fhir(sr)


@router.post("/ServiceRequest/{sr_id}/$result",
             summary="Execute the order: generate results for it",
             description="FHIR-like operation: produces a Bundle containing "
                         "DiagnosticReport + Observation resources with randomly "
                         "generated values for this order's sample. Both reference the "
                         "order in basedOn and can be read back by id.")
def execute_service_request(sr_id: str, request: Request):
    sr = SERVICE_REQUESTS.get(sr_id)
    if not sr:
        raise HTTPException(404, f"ServiceRequest/{sr_id} not found")
    report, observations = _generate(sr["sample_id"], get_device(sr["device_id"]),
                                     based_on=f"ServiceRequest/{sr_id}")
    sr["status"] = "completed"
    return F.bundle("collection", _base(request), [report] + observations)


# ---------------------------------------------------------------------------
# Observation / DiagnosticReport
# ---------------------------------------------------------------------------

def _sample_param(sample: str | None, specimen: str | None, subject: str | None) -> str:
    sample_id = sample or specimen or subject
    if not sample_id:
        raise HTTPException(400, "Provide ?sample=<id> (aliases: ?specimen=, ?subject=)")
    return sample_id.split("|")[-1]  # accept token form system|value


@router.get("/Observation", summary="Generate observations for a sample",
            description="query: ?sample=S-2026-12345 (aliases ?specimen=, ?subject=), "
                        "optional ?device=ANA-002 (default: a random FHIR device). Every "
                        "call generates a fresh random result set, as if the instrument "
                        "had just finished the run.")
def search_observations(request: Request,
                        sample: str | None = Query(default=None),
                        specimen: str | None = Query(default=None),
                        subject: str | None = Query(default=None),
                        device: str | None = Query(default=None)):
    sample_id = _sample_param(sample, specimen, subject)
    _, observations = _generate(sample_id, _fhir_device(device))
    return F.bundle("searchset", _base(request), observations)


@router.get("/Observation/{obs_id}", summary="Read a generated or created Observation")
def read_observation(obs_id: str):
    return _read("Observation", obs_id)


@router.post("/Observation", status_code=201,
             summary="Create an Observation (stored as sent, for integration testing)")
def create_observation(request: Request, body: dict):
    if body.get("resourceType", "Observation") != "Observation":
        raise HTTPException(400, f"Expected an Observation, got {body['resourceType']}")
    body["resourceType"] = "Observation"
    body["id"] = F.new_id("OBS")
    body.setdefault("status", "final")
    body["meta"] = {**(body.get("meta") or {}), "lastUpdated": F.fhir_datetime()}
    _store([body])
    return _created(request, body)


@router.get("/DiagnosticReport", summary="Generate a DiagnosticReport for a sample",
            description="Same parameters as GET /Observation. Returns a searchset with "
                        "the report (match) and its Observations (include).")
def search_reports(request: Request,
                   sample: str | None = Query(default=None),
                   specimen: str | None = Query(default=None),
                   subject: str | None = Query(default=None),
                   device: str | None = Query(default=None)):
    sample_id = _sample_param(sample, specimen, subject)
    report, observations = _generate(sample_id, _fhir_device(device))
    return F.bundle("searchset", _base(request), [report], includes=observations)


@router.get("/DiagnosticReport/{report_id}", summary="Read a generated DiagnosticReport")
def read_report(report_id: str):
    return _read("DiagnosticReport", report_id)
