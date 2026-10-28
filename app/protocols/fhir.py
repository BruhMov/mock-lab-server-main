"""FHIR R4 resource builders for the mock equipment.

Minimal but valid resources: Device, ServiceRequest, Observation,
DiagnosticReport, Bundle, OperationOutcome, CapabilityStatement — enough for
a LIMS to exercise a FHIR-based integration.
"""

from __future__ import annotations

import itertools
import random
from datetime import datetime

from ..config import Device, DEVICES
from ..simulator import now_ts

_rng = random.Random()
_ids = itertools.count(1)

SYSTEM_LOINC = "http://loinc.org"
SYSTEM_LOCAL = "http://mock-lab.local/fhir/CodeSystem/local"
SYSTEM_UCUM = "http://unitsofmeasure.org"

# Simulator unit -> UCUM code. Units missing here have no UCUM code and are sent
# as text only (absorbance "A" would read as ampere in UCUM).
UCUM = {
    "%": "%", "10^9/L": "10*9/L", "10^12/L": "10*12/L", "U/L": "U/L", "deg": "deg",
    "degC": "Cel", "g": "g", "g/L": "g/L", "mV": "mV", "mg/L": "mg/L", "min": "min",
    "mmol/L": "mmol/L", "pH": "[pH]", "umol/L": "umol/L",
}

# LOINC codes for the clinical analytes, matching the units the simulator uses
LOINC = {
    "GLU": ("14749-6", "Glucose [Moles/volume] in Serum or Plasma"),
    "ALT": ("1742-6", "Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma"),
    "AST": ("1920-8", "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma"),
    "CREA": ("14682-9", "Creatinine [Moles/volume] in Serum or Plasma"),
    "CHOL": ("14647-2", "Cholesterol [Moles/volume] in Serum or Plasma"),
    "ALB": ("1751-7", "Albumin [Mass/volume] in Serum or Plasma"),
    "WBC": ("6690-2", "Leukocytes [#/volume] in Blood by Automated count"),
    "RBC": ("789-8", "Erythrocytes [#/volume] in Blood by Automated count"),
    "HGB": ("718-7", "Hemoglobin [Mass/volume] in Blood"),
    "PLT": ("777-3", "Platelets [#/volume] in Blood by Automated count"),
    "HCT": ("4544-3", "Hematocrit [Volume Fraction] of Blood by Automated count"),
}

ISSUE_CODES = {400: "invalid", 404: "not-found", 405: "not-supported",
               409: "conflict", 422: "processing"}


def fhir_devices() -> list[Device]:
    return [d for d in DEVICES if "fhir" in d.protocols]


def new_id(prefix: str) -> str:
    return f"{prefix}-{next(_ids):06d}"


def fhir_datetime(ts: str | None = None) -> str:
    """FHIR dateTime/instant with the local UTC offset (FHIR requires a time
    zone whenever a time is given). `ts` is a naive ISO timestamp; default now."""
    dt = datetime.fromisoformat(ts) if ts else datetime.now()
    return dt.astimezone().isoformat(timespec="seconds")


def quantity(value: float, unit: str) -> dict:
    q = {"value": value, "unit": unit}
    if unit in UCUM:
        q.update(system=SYSTEM_UCUM, code=UCUM[unit])
    return q


def sample_identifier(sample_id: str) -> dict:
    return {"system": SYSTEM_LOCAL, "value": sample_id}


def specimen_ref(sample_id: str) -> dict:
    """Logical reference to the sample by its identifier (no Specimen resource is served)."""
    return {"type": "Specimen", "identifier": sample_identifier(sample_id), "display": sample_id}


def device_to_fhir(device: Device) -> dict:
    return {
        "resourceType": "Device",
        "id": device.device_id,
        "meta": {"source": "#mock-lab-server"},
        "manufacturer": device.manufacturer,
        "modelNumber": device.model,
        "serialNumber": device.serial,
        "deviceName": [
            {"name": device.name, "type": "user-friendly-name"},
            {"name": device.model, "type": "model-name"},
        ],
        "location": {"display": device.location},
        "status": "active",
        "note": [
            {"text": f"Simulated instrument. Supported protocols: {', '.join(device.protocols)}"}
        ],
    }


def service_request_to_fhir(sr: dict) -> dict:
    return {
        "resourceType": "ServiceRequest",
        "id": sr["id"],
        "identifier": [{"system": SYSTEM_LOCAL, "value": sr["id"]}],
        "status": sr.get("status", "active"),
        "intent": "order",
        "code": {"coding": [{"system": SYSTEM_LOCAL, "code": sr["panel"], "display": sr["panel"]}],
                 "text": sr["panel"]},
        "subject": {"identifier": sample_identifier(sr["sample_id"]), "display": "Doe, John"},
        "specimen": [specimen_ref(sr["sample_id"])],
        "authoredOn": fhir_datetime(sr.get("authored_on")),
        "requester": {"display": sr.get("requester", "Dr. House")},
        "performer": [{"reference": f"Device/{sr['device_id']}", "display": sr["device_id"]}],
        "extension": [{
            "url": f"{SYSTEM_LOCAL}/target-device",
            "valueReference": {"reference": f"Device/{sr['device_id']}", "display": sr["device_id"]},
        }],
    }


def _observation(device: Device, m: dict, v: dict, based_on: str | None) -> dict:
    val = round(float(v["value"]), v.get("decimals", 2))
    coding = [{"system": SYSTEM_LOCAL, "code": v["code"], "display": v["name"]}]
    if v["code"] in LOINC:
        code, display = LOINC[v["code"]]
        coding.append({"system": SYSTEM_LOINC, "code": code, "display": display})
    obs = {
        "resourceType": "Observation",
        "id": new_id("OBS"),
        "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
            "code": "laboratory", "display": "Laboratory"}]}],
        "code": {"coding": coding, "text": v["name"]},
        "subject": {"identifier": sample_identifier(m["sample_id"])},
        "effectiveDateTime": fhir_datetime(m["measured_at"]),
        "issued": fhir_datetime(),
        "valueQuantity": quantity(val, v["unit"]),
        "specimen": specimen_ref(m["sample_id"]),
        "device": {"reference": f"Device/{device.device_id}"},
    }
    if based_on:
        obs["basedOn"] = [{"reference": based_on}]
    if v.get("ref_range"):
        lo_hi = str(v["ref_range"]).split("-")
        if len(lo_hi) == 2:
            obs["referenceRange"] = [{
                "low": quantity(float(lo_hi[0]), v["unit"]),
                "high": quantity(float(lo_hi[1]), v["unit"]),
            }]
    if v.get("flag"):
        interp = "H" if v["flag"] == "H" else "L"
        obs["interpretation"] = [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
            "code": interp, "display": "High" if interp == "H" else "Low"}]}]
    return obs


def measurement_to_observations(device: Device, m: dict, based_on: str | None = None) -> list[dict]:
    """One Observation per measured value; `based_on` is e.g. "ServiceRequest/SR-1000"."""
    return [_observation(device, m, v, based_on) for v in m["values"]]


def measurement_to_diagnostic_report(device: Device, m: dict, observations: list[dict],
                                     based_on: str | None = None) -> dict:
    report = {
        "resourceType": "DiagnosticReport",
        "id": new_id("RPT"),
        "status": "final",
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": "LAB", "display": "Laboratory"}]}],
        "code": {"coding": [{"system": SYSTEM_LOCAL, "code": m["panel"], "display": m["panel"]}],
                 "text": m["panel"]},
        "subject": {"identifier": sample_identifier(m["sample_id"])},
        "effectiveDateTime": fhir_datetime(m["measured_at"]),
        "issued": fhir_datetime(),
        "specimen": [specimen_ref(m["sample_id"])],
        "result": [{"reference": f"Observation/{o['id']}"} for o in observations],
        "conclusion": f"Simulated {device.name} run for sample {m['sample_id']}.",
    }
    if based_on:
        report["basedOn"] = [{"reference": based_on}]
    return report


def bundle(bundle_type: str, base_url: str, matches: list[dict],
           includes: list[dict] | None = None) -> dict:
    """Bundle whose entries carry fullUrl (base_url + Type/id), so the relative
    references between them resolve. Searchsets mark entries match/include."""
    def entry(resource: dict, mode: str) -> dict:
        e = {"fullUrl": f"{base_url}{resource['resourceType']}/{resource['id']}", "resource": resource}
        if bundle_type == "searchset":
            e["search"] = {"mode": mode}
        return e

    out = {"resourceType": "Bundle", "id": new_id("BUNDLE"), "type": bundle_type,
           "timestamp": fhir_datetime()}
    if bundle_type == "searchset":
        out["total"] = len(matches)
    out["entry"] = ([entry(r, "match") for r in matches]
                    + [entry(r, "include") for r in includes or []])
    return out


def operation_outcome(status_code: int, diagnostics: str) -> dict:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": ISSUE_CODES.get(status_code, "exception"),
                   "diagnostics": str(diagnostics)}],
    }


def capability_statement(base_url: str) -> dict:
    sample_params = [
        {"name": "sample", "type": "string", "documentation": "Sample id (generates fresh results)"},
        {"name": "specimen", "type": "string", "documentation": "Alias of sample"},
        {"name": "subject", "type": "string", "documentation": "Alias of sample"},
        {"name": "device", "type": "reference", "documentation": "Device id, e.g. ANA-002"},
    ]
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": fhir_datetime()[:10],
        "publisher": "Mock Lab Equipment Server",
        "kind": "instance",
        "software": {"name": "mock-lab-equipment", "version": "1.0.0"},
        "implementation": {"description": "Mock Laboratory Equipment Server",
                           "url": base_url.rstrip("/")},
        "fhirVersion": "4.0.1",
        "format": ["json", "application/fhir+json"],
        "rest": [{
            "mode": "server",
            "resource": [
                {"type": "Device", "interaction": [{"code": "read"}, {"code": "search-type"}]},
                {"type": "ServiceRequest",
                 "interaction": [{"code": "read"}, {"code": "search-type"}, {"code": "create"}],
                 "searchParam": [{"name": "status", "type": "token"}],
                 "operation": [{"name": "result", "definition": f"{SYSTEM_LOCAL}/ServiceRequest-$result"}]},
                {"type": "Observation",
                 "interaction": [{"code": "read"}, {"code": "search-type"}, {"code": "create"}],
                 "searchParam": sample_params},
                {"type": "DiagnosticReport",
                 "interaction": [{"code": "read"}, {"code": "search-type"}],
                 "searchParam": sample_params},
            ],
        }],
    }


def seed_service_requests(n: int = 6) -> list[dict]:
    """Create a few in-memory ServiceRequests spread across the FHIR devices."""
    devices = fhir_devices()
    reqs = []
    for i in range(n if devices else 0):
        dev = devices[i % len(devices)]
        reqs.append({
            "id": f"SR-{1000 + i}",
            "device_id": dev.device_id,
            "sample_id": f"S-2026-{20000 + i * 137}",
            "panel": dev.panel,
            "status": "active",
            "authored_on": now_ts("%Y-%m-%dT%H:%M:%S"),
            "requester": _rng.choice(["Dr. Ivanov", "Dr. Petrova", "QC-Manager"]),
        })
    return reqs
