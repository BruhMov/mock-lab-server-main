"""FHIR R4 surface: formats, references, ServiceRequest intake, OperationOutcome."""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.protocols import fhir as F

# TestClient outside a `with` block does not run the lifespan, so no listeners start
client = TestClient(app)
FHIR_JSON = "application/fhir+json"
DATETIME_WITH_TZ = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(Z|[+-]\d\d:\d\d)$")


def datetimes(obj, keys=("effectiveDateTime", "issued", "timestamp", "authoredOn", "lastUpdated")):
    """All values of time-typed elements anywhere in a resource."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys:
                yield v
            yield from datetimes(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from datetimes(v, keys)


def resources(bundle: dict, rtype: str) -> list[dict]:
    return [e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == rtype]


def assert_references_resolve(bundle: dict):
    """Every relative reference to a bundled resource matches some entry's fullUrl."""
    full_urls = {e["fullUrl"] for e in bundle["entry"]}
    base = next(iter(full_urls)).rsplit("/", 2)[0] + "/"
    for e in bundle["entry"]:
        r = e["resource"]
        assert e["fullUrl"] == f"{base}{r['resourceType']}/{r['id']}"
        refs = [x["reference"] for x in r.get("result", [])]
        for ref in refs:
            assert base + ref in full_urls, ref


def test_metadata():
    resp = client.get("/fhir/metadata")
    assert resp.headers["content-type"].startswith(FHIR_JSON)
    cs = resp.json()
    assert cs["kind"] == "instance" and cs["implementation"]["url"].endswith("/fhir")
    assert "application/fhir+json" in cs["format"]


def test_observation_search():
    resp = client.get("/fhir/Observation", params={"sample": "S-OBS-1", "device": "ANA-002"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith(FHIR_JSON)
    b = resp.json()
    assert b["type"] == "searchset" and b["total"] == len(b["entry"]) == 5
    assert all(e["search"]["mode"] == "match" and e["fullUrl"] for e in b["entry"])
    assert all(DATETIME_WITH_TZ.match(t) for t in datetimes(b))
    wbc = resources(b, "Observation")[0]
    assert wbc["specimen"]["identifier"]["value"] == "S-OBS-1"
    assert wbc["valueQuantity"]["unit"] == "10^9/L"
    assert wbc["valueQuantity"]["code"] == "10*9/L"
    assert wbc["valueQuantity"]["system"] == F.SYSTEM_UCUM
    assert {"system": F.SYSTEM_LOINC, "code": "6690-2"}.items() <= wbc["code"]["coding"][1].items()
    # generated Observations can be read back
    assert client.get(f"/fhir/Observation/{wbc['id']}").json() == wbc


def test_unit_without_ucum_code_is_text_only():
    b = client.get("/fhir/Observation", params={"sample": "S-1", "device": "SPC-001"}).json()
    absorbance = next(o for o in resources(b, "Observation") if o["code"]["coding"][0]["code"] == "ABS")
    assert absorbance["valueQuantity"]["unit"] == "A"
    assert "system" not in absorbance["valueQuantity"] and "code" not in absorbance["valueQuantity"]


def test_diagnostic_report_search_is_a_resolvable_searchset():
    b = client.get("/fhir/DiagnosticReport", params={"specimen": "S-RPT-1"}).json()
    assert b["resourceType"] == "Bundle" and b["type"] == "searchset" and b["total"] == 1
    (report,) = resources(b, "DiagnosticReport")
    assert [e["search"]["mode"] for e in b["entry"]] == ["match"] + ["include"] * len(report["result"])
    assert_references_resolve(b)
    assert client.get(f"/fhir/DiagnosticReport/{report['id']}").status_code == 200


def test_fhir_service_request_is_honoured():
    sr = {
        "resourceType": "ServiceRequest", "status": "active", "intent": "order",
        "code": {"coding": [{"system": F.SYSTEM_LOCAL, "code": "HEMOGRAM"}]},
        "subject": {"reference": "Patient/123"},
        "specimen": [{"identifier": {"system": F.SYSTEM_LOCAL, "value": "S-FHIR-77"}}],
        "performer": [{"reference": "Device/ANA-002"}],
        "requester": {"display": "Dr. Who"},
    }
    resp = client.post("/fhir/ServiceRequest", json=sr, headers={"Content-Type": FHIR_JSON})
    assert resp.status_code == 201
    created = resp.json()
    assert resp.headers["location"].endswith(f"/fhir/ServiceRequest/{created['id']}")
    assert created["specimen"][0]["identifier"]["value"] == "S-FHIR-77"
    assert created["performer"][0]["reference"] == "Device/ANA-002"
    assert created["code"]["coding"][0]["code"] == "HEMOGRAM"
    assert created["requester"]["display"] == "Dr. Who"

    b = client.post(f"/fhir/ServiceRequest/{created['id']}/$result").json()
    assert b["type"] == "collection"
    assert_references_resolve(b)
    (report,) = resources(b, "DiagnosticReport")
    assert report["basedOn"] == [{"reference": f"ServiceRequest/{created['id']}"}]
    assert all(o["basedOn"] == report["basedOn"] for o in resources(b, "Observation"))
    assert report["specimen"][0]["identifier"]["value"] == "S-FHIR-77"
    assert client.get(f"/fhir/ServiceRequest/{created['id']}").json()["status"] == "completed"


def test_simplified_service_request_body_still_works():
    created = client.post("/fhir/ServiceRequest",
                          json={"sample_id": "S-OLD-1", "device": "CHR-002", "panel": "X"}).json()
    assert created["subject"]["identifier"]["value"] == "S-OLD-1"
    assert created["performer"][0]["reference"] == "Device/CHR-002"
    assert created["code"]["text"] == "X"


@pytest.mark.parametrize("method,path,kwargs,status,code", [
    ("get", "/fhir/Device/NOPE", {}, 404, "not-found"),
    ("get", "/fhir/Observation/NOPE", {}, 404, "not-found"),
    ("get", "/fhir/Patient", {}, 404, "not-found"),
    ("get", "/fhir/Observation", {}, 400, "invalid"),
    ("post", "/fhir/ServiceRequest", {"json": {"performer": [{"reference": "Device/NOPE"}]}}, 422, "processing"),
    ("post", "/fhir/ServiceRequest", {"json": {"device": "BAL-001"}}, 422, "processing"),
    ("post", "/fhir/ServiceRequest", {"json": {"resourceType": "Patient"}}, 400, "invalid"),
    ("post", "/fhir/Observation", {"content": b"not json", "headers": {"Content-Type": FHIR_JSON}}, 400, "invalid"),
])
def test_errors_are_operation_outcomes(method, path, kwargs, status, code):
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith(FHIR_JSON)
    oo = resp.json()
    assert oo["resourceType"] == "OperationOutcome" and oo["issue"][0]["code"] == code


def test_non_fhir_errors_keep_fastapi_format():
    resp = client.get("/api/devices/NOPE")
    assert resp.status_code == 404 and "detail" in resp.json()


def test_created_observation_is_stored():
    resp = client.post("/fhir/Observation", json={"code": {"text": "x"}, "valueString": "ok"})
    assert resp.status_code == 201
    obs = resp.json()
    assert client.get(f"/fhir/Observation/{obs['id']}").json()["valueString"] == "ok"
    assert DATETIME_WITH_TZ.match(obs["meta"]["lastUpdated"])
