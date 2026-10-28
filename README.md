# Mock Laboratory Equipment Server

A self-contained **FastAPI** server that simulates laboratory equipment
(весы, анализаторы, хроматографы, pH-метры, спектрофотометры) so you can plan
and test a **LIMS integration** before real instruments exist.

Every instrument generates **random but realistic measurement data on request**,
instantly and synchronously, and serves it over four integration protocols.

---

## Quick start

```bash
pip install -r requirements.txt
./run.sh                      # or: python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

| Listener | Address |
|---|---|
| HTTP REST + Swagger UI | `http://<host>:8000` (interactive docs at `/docs`) |
| Raw ASTM E1381/E1394 TCP | `tcp://<host>:5150` |
| HL7 v2 MLLP TCP | `tcp://<host>:2575` |

Open `http://localhost:8000/docs` — every endpoint can be exercised from the browser.

### Configuration

All settings are environment variables; nothing has to be edited in code.

| Variable | Default | Meaning |
|---|---|---|
| `MOCK_HTTP_HOST` / `MOCK_HTTP_PORT` | `0.0.0.0` / `8000` | HTTP address (read by `run.sh`) |
| `MOCK_ASTM_HOST` / `MOCK_ASTM_PORT` | `0.0.0.0` / `5150` | raw ASTM TCP listener |
| `MOCK_HL7_HOST` / `MOCK_HL7_PORT` | `0.0.0.0` / `2575` | HL7 MLLP listener |
| `MOCK_DEVICES_FILE` | built-in 7 devices | JSON file that replaces the device list; start from `devices.example.json` |
| `MOCK_LOG_LEVEL` | `INFO` | `DEBUG` also logs every ASTM frame and HL7 message sent and received |
| `MOCK_PUSH_ALLOWED_HOSTS` | any host | comma-separated host names, IPs and CIDR networks that `push-tcp` / `push-mllp` may connect to, e.g. `10.0.5.0/24,lims-test.local`; anything else gets 403 |
| `MOCK_RELOAD` | `0` | `1` restarts the server on code changes (`run.sh`, development only) |

```bash
MOCK_LOG_LEVEL=DEBUG MOCK_PUSH_ALLOWED_HOSTS=10.0.5.0/24 MOCK_DEVICES_FILE=lab.json ./run.sh
```

An invalid value stops the server at start with a message naming the variable.
On a shared stand set `MOCK_PUSH_ALLOWED_HOSTS`: without it the push helpers
can open TCP connections to any host reachable from the server.

---

## Simulated instruments

| ID | Type | Manufacturer / Model | Protocols |
|---|---|---|---|
| `BAL-001` | Analytical balance / Лабораторные весы | Mettler Toledo XPR226DR | REST, LabX, ASTM (+TCP) |
| `CHR-001` | Gas chromatograph / Газовый хроматограф | Agilent 8890 GC | REST, ASTM (+TCP), FHIR |
| `CHR-002` | HPLC / Жидкостный хроматограф | Shimadzu LC-2050C | REST, LabX, FHIR |
| `ANA-001` | Biochemistry analyzer / Биохимический анализатор | Roche cobas c311 | REST, ASTM (+TCP), HL7v2 (+MLLP) |
| `ANA-002` | Hematology analyzer / Гематологический анализатор | Sysmex XN-1000 | REST, ASTM (+TCP), HL7v2 (+MLLP), FHIR |
| `PHM-001` | pH meter / pH-метр | Mettler SevenExcellence S400 | REST, LabX |
| `SPC-001` | UV spectrophotometer / Спектрофотометр | Shimadzu UV-1900i | REST, FHIR, LabX |

What the random generator produces per type:

* **Balance** — gross/tare/net weight (0.1 mg readability), stability flag,
  ambient temperature.
* **Chromatograph (GC/HPLC)** — 3–7 peaks with retention time, area, area %,
  height, resolution, tailing, plates; assay %, purity angle, plus a
  synthesized chromatogram point array; ~15 % of assays are flagged out of range.
* **Analyzers** — biochem panel (GLU, ALT, AST, CREA, CHOL, ALB) or hemogram
  (WBC, RBC, HGB, PLT, HCT) with units, reference ranges and random H/L flags.
* **pH meter** — pH with H/L flag, temperature, mV, 3-point calibration record.
* **Spectrophotometer** — absorbance, Beer-Lambert concentration, UV scan
  (220–700 nm) around a random λ-max.

---

## Endpoint map

### 1. Generic REST (`/api`) — the simplest surface

| Method & path | Purpose |
|---|---|
| `GET /api/health` | liveness |
| `GET /api/devices` | instrument inventory |
| `GET /api/devices/{id}` | one instrument |
| `POST /api/devices/{id}/measure` | **instant random measurement** (JSON body `{"sample_id": "..."}` optional) |
| `GET /api/worklist/{id}` | simulated waiting orders |

```bash
curl -X POST http://localhost:8000/api/devices/CHR-002/measure \
     -H 'Content-Type: application/json' -d '{"sample_id":"B-2291"}'
```

Response contains values with units/reference ranges/flags plus
type-specific extras (peaks, chromatogram, calibration, scan…).

### 2. ASTM E1394 (`/astm` + raw TCP :5150)

| Method & path | Purpose |
|---|---|
| `GET /astm/devices` | which instruments speak ASTM |
| `POST /astm/devices/{id}/message?sample_id=&framing=records\|e1381` | H/P/O/R/L records as text (or E1381 frames) |
| `POST /astm/devices/{id}/frames?sample_id=` | raw framed payload (`text/plain`) |
| `POST /astm/devices/{id}/push-tcp?host=&port=` | mock device dials **your** LIMS TCP listener (ENQ/ACK/EOT session) |

Example message (records mode):

```
H|\^&|||ANA-001^cobas c311^Roche|||||LIMS||P|E1394-97|20261027142655
P|1||S-2026-77||Doe^John||19800101|U
O|1|S-2026-77||^^^BIOCHEM|R|20261027142655|||||N||||SERUM||||||||||F
R|1|^^^GLU|5.69|mmol/L|3.9-6.4|||F||smirnov_s||20261027142655|cobas c311
L|1|N
```

Field positions follow E1394: sender in H.5, receiver in H.10, specimen id in
O.3, result status in R.9. Delimiters inside values are escaped (`10^9/L` is
sent as `10&S&9/L`); chromatography peaks travel in manufacturer `M` records.

E1381 framing: `<STX>FN<record><CR><ETX><C1C2><CR><LF>`, checksum = sum of
FN..ETX modulo 256, FN runs 1..7, 0, 1…; records over 240 characters continue
in `<ETB>` frames.

Raw TCP session (device side) — connect to `:5150` and act as the host:

```
host -> ENQ            device -> ACK
host -> STX frames (H, Q, L)   device -> ACK per frame
host -> EOT
device -> ENQ          host -> ACK
device -> result frames (H, P, O, R..., L)   host -> ACK per frame
device -> EOT
```

The answering device is the one named in the host's H.10 receiver ID (e.g.
`H|\^&|||LIMS|||||ANA-001`), otherwise `BAL-001`. Results are generated for the
specimen id in Q.3 (`Q|1|^S-2026-77||^^^ALL`). A frame with a bad checksum is
answered with NAK; a result frame the host NAKs is resent up to 6 times.

A working host-side example client is in `scripts/test_tcp_clients.py`.

### 3. HL7 v2.5 (`/hl7` + MLLP TCP :2575)

| Method & path | Purpose |
|---|---|
| `GET /hl7/devices` | which instruments speak HL7v2 |
| `POST /hl7/devices/{id}/oru?sample_id=&mllp=` | unsolicited **ORU^R01** (optionally MLLP-wrapped bytes) |
| `POST /hl7/devices/{id}/query` | send **QBP^Q11** (body) → receive **ORU^R01** |
| `POST /hl7/devices/{id}/push-mllp?host=&port=` | push ORU^R01 to **your** MLLP listener, report the ACK (`accepted` / `rejected` / `no_ack`) |

MLLP framing on :2575: `<VT> message <FS><CR>` (`0x0B … 0x1C 0x0D`); the stream
is split on `<FS><CR>`, so a message may span several TCP segments and several
messages may share one.
Queries containing `QPD`/`QRD` are answered with ORU^R01 for the parsed sample
id; any other inbound message receives an `MSA|AA` acknowledgment. The answering
device is the one named in MSH-6 (or MSH-5) by device id or model, otherwise
`ANA-001`.

### 4. FHIR R4 (`/fhir`)

| Method & path | Purpose |
|---|---|
| `GET /fhir/metadata` | CapabilityStatement |
| `GET /fhir/Device` / `GET /fhir/Device/{id}` | instrument registry |
| `GET/POST /fhir/ServiceRequest` · `GET /fhir/ServiceRequest/{id}` | lab orders (seeded + creatable) |
| `POST /fhir/ServiceRequest/{id}/$result` | execute order → DiagnosticReport + Observations (`basedOn` the order) |
| `GET /fhir/Observation?sample=S-2026-77&device=ANA-002` | fresh random Observation searchset |
| `GET /fhir/DiagnosticReport?sample=...&device=...` | searchset: report + its Observations |
| `GET /fhir/Observation/{id}` · `GET /fhir/DiagnosticReport/{id}` | read a generated resource back |
| `POST /fhir/Observation` | store an Observation as sent (integration testing) |

FHIR details:

* Responses are `application/fhir+json`; errors under `/fhir` are
  `OperationOutcome` (404 not-found, 400 invalid, 422 processing).
* `POST /fhir/ServiceRequest` takes a regular FHIR ServiceRequest: sample from
  `specimen[0].identifier.value` (or `subject.identifier.value`), device from
  `performer` (`Device/ANA-002`), panel from `code.coding[0].code`. The
  simplified body `{"sample_id", "device", "panel", "requester"}` still works.
* Only FHIR-capable devices (see the table above) take orders and produce
  results; without `device` a random one is used.
* Times carry the UTC offset; quantities carry UCUM codes (`10^9/L` →
  `10*9/L`, `degC` → `Cel`; absorbance `A` has no UCUM code and is sent as
  text); clinical analytes also carry LOINC codes. Bundle entries have
  `fullUrl`, so references between them resolve.

### 5. LabX-style (`/labx/v1`)

Mimics a metrology/quality data system: instruments → methods → orders →
executions → results. Send `Accept: application/xml` on any GET for XML.

| Method & path | Purpose |
|---|---|
| `GET /labx/v1/instruments` | instrument list (status, last calibration) |
| `GET /labx/v1/instruments/{id}/methods` | installed methods |
| `GET /labx/v1/orders` · `POST /labx/v1/orders` | list / create orders |
| `POST /labx/v1/orders/{id}/execution` | run the order → random results |
| `GET /labx/v1/orders/{id}/results` | fetch results of a completed order |
| `POST /labx/v1/results` | dummy sink for testing your LIMS result-acceptance endpoint |

---

## Testing your LIMS's own TCP/MLLP listener

The push helpers let the mock device play *client* while your LIMS plays host:

```bash
# ASTM: device dials your listener and runs a full ENQ/ACK/EOT session
curl -X POST "http://localhost:8000/astm/devices/ANA-001/push-tcp?host=10.0.0.5&port=5100"

# HL7: device pushes an ORU^R01 into your MLLP listener and reports the MSA ack
curl -X POST "http://localhost:8000/hl7/devices/ANA-002/push-mllp?host=10.0.0.5&port=6661"
```

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest                      # unit + localhost TCP session tests
python scripts/test_tcp_clients.py    # smoke test against a running server
```

## Project layout

```
app/
├── main.py            FastAPI app, lifespan starts both TCP listeners
├── config.py          device registry + MOCK_* environment settings
├── simulator.py       random measurement generators (one per equipment type)
├── protocols/
│   ├── astm.py        E1394 records + E1381 framing/checksums
│   ├── hl7.py         ORU^R01 / QBP parsing / MSA acks / MLLP wrap
│   ├── fhir.py        Device, ServiceRequest, Observation, DiagnosticReport
│   └── labx.py        LabX-style resources + XML serializer
├── routers/           /api, /astm, /hl7, /fhir, /labx/v1
└── tcp/               raw ASTM :5150 and HL7 MLLP :2575 asyncio listeners,
                       outbound.py: push connections checked against the allowlist
devices.example.json          the built-in device list, as a MOCK_DEVICES_FILE template
scripts/test_tcp_clients.py   host-side ASTM + MLLP smoke tests
tests/                        pytest suite (framing, field positions, TCP sessions, push helpers)
```

## Extending

* **New instrument** — copy `devices.example.json`, add an entry and point
  `MOCK_DEVICES_FILE` at the copy (or edit `DEFAULT_DEVICES` in
  `app/config.py`); if its `type` matches an existing generator you get all
  protocols for free.
* **New equipment type** — add a `_generator` function in `app/simulator.py`
  and register it in `_GENERATORS`; ASTM/HL7/FHIR renderers pick it up
  automatically.
* **Ports** — `MOCK_ASTM_PORT` / `MOCK_HL7_PORT` (see Configuration).
