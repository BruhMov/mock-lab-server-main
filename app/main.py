"""Mock Laboratory Equipment Server — FastAPI application.

HTTP REST + Swagger UI on :8000, raw ASTM TCP on :5150, HL7 MLLP on :2575
(ports and the rest of the settings: MOCK_* environment variables, see README).
Run:  ./run.sh   or   python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler, request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import (
    ASTM_TCP_HOST, ASTM_TCP_PORT, DEVICES, HL7_MLLP_HOST, HL7_MLLP_PORT, LOG_LEVEL,
    PUSH_ALLOWED_HOSTS,
)
from .protocols.fhir import operation_outcome
from .routers import astm, fhir, hl7, labx, rest
from .tcp.astm_tcp import start_astm_listener
from .tcp.hl7_mllp import start_hl7_listener

DESCRIPTION = """
Mock server that **simulates laboratory equipment** for planning and testing
LIMS integration. Every instrument generates **random but realistic data** on
request — instant, synchronous responses.

**Equipment types** (7 instruments):
* Весы / Balances — mass with stability & drift
* Хроматографы / GC + HPLC — peaks, retention times, area %, chromatogram trace
* Анализаторы / Biochem + Hematology analyzers — analyte panels with H/L flags
* pH-метры / Спектрофотометры — pH with calibration, UV absorbance scans

**Protocol surfaces**
| Surface | Where | Notes |
|---|---|---|
| Generic REST | `/api` | device inventory, instant measurements |
| ASTM E1394 | `/astm` + raw TCP :5150 | H/P/O/R/L records, E1381 framing |
| HL7 v2.5 | `/hl7` + MLLP TCP :2575 | ORU^R01, QBP handling, MSA acks |
| FHIR R4 | `/fhir` | Device, ServiceRequest, Observation, DiagnosticReport |
| LabX-style | `/labx/v1` | instruments, methods, orders, executions, results |

Interactive testing: use this Swagger UI or the device-agnostic push helpers
(`/astm/devices/{id}/push-tcp`, `/hl7/devices/{id}/push-mllp`) to test your
LIMS-side TCP/MLLP listeners.
"""


logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logger = logging.getLogger("mock_lab")


@asynccontextmanager
async def lifespan(app: FastAPI):
    astm_server = await start_astm_listener()
    hl7_server = await start_hl7_listener()
    logger.info("Mock laboratory equipment server started: %d devices, Swagger UI at /docs, "
                "ASTM tcp://%s:%s, HL7 MLLP tcp://%s:%s", len(DEVICES),
                ASTM_TCP_HOST, ASTM_TCP_PORT, HL7_MLLP_HOST, HL7_MLLP_PORT)
    if PUSH_ALLOWED_HOSTS is None:
        logger.warning("push-tcp/push-mllp may connect to any host; "
                       "set MOCK_PUSH_ALLOWED_HOSTS to limit them")
    yield
    astm_server.close()
    hl7_server.close()
    await astm_server.wait_closed()
    await hl7_server.wait_closed()
    logger.info("Mock laboratory equipment server stopped.")


app = FastAPI(
    title="Mock Laboratory Equipment Server",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(rest.router)
app.include_router(astm.router)
app.include_router(hl7.router)
app.include_router(fhir.router)
app.include_router(labx.router)


# Errors under /fhir are returned as FHIR OperationOutcome; elsewhere FastAPI's default
def _is_fhir(request: Request) -> bool:
    return request.url.path.startswith("/fhir")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if _is_fhir(request):
        return fhir.FHIRResponse(operation_outcome(exc.status_code, exc.detail),
                                 status_code=exc.status_code, headers=exc.headers)
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if _is_fhir(request):
        details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return fhir.FHIRResponse(operation_outcome(400, details), status_code=400)
    return await request_validation_exception_handler(request, exc)


@app.get("/", tags=["core"], include_in_schema=False)
def root():
    return {
        "service": "mock-laboratory-equipment",
        "docs": "/docs",
        "endpoints": ["/api", "/astm", "/hl7", "/fhir", "/labx/v1"],
        "tcp": {"astm": ASTM_TCP_PORT, "hl7_mllp": HL7_MLLP_PORT},
    }
