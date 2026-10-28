"""Device registry and runtime settings of the mock laboratory equipment server.

Each device entry describes a simulated instrument: its type (which drives the
random data generator), the integration protocols it exposes, and metadata that
is echoed into ASTM / HL7 / FHIR / LabX payloads so the LIMS sees realistic
sender identification.

Settings come from environment variables (see README, "Configuration"); the
device list can be replaced by a JSON file named in MOCK_DEVICES_FILE.
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass, field, fields

DEVICE_TYPES = {"balance", "chromatograph_gc", "chromatograph_hplc", "analyzer_chem",
                "analyzer_hema", "ph_meter", "spectrophotometer"}
PROTOCOLS = {"rest", "astm", "hl7v2", "fhir", "labx"}


@dataclass(frozen=True)
class Device:
    device_id: str            # short id used in every URL, e.g. BAL-001
    type: str                 # balance | chromatograph_gc | chromatograph_hplc |
                              # analyzer_chem | analyzer_hema | ph_meter | spectrophotometer
    name: str                 # bilingual display name
    manufacturer: str
    model: str
    serial: str
    location: str
    protocols: list[str] = field(default_factory=list)   # subset of: rest, astm, hl7v2, fhir, labx
    tcp_enabled: bool = False  # device reachable over its raw TCP/MLLP listener
    panel: str = "ROUTINE"     # default order panel / method name


DEFAULT_DEVICES: list[Device] = [
    Device(
        device_id="BAL-001",
        type="balance",
        name="Analytical Balance / Лабораторные весы",
        manufacturer="Mettler Toledo",
        model="XPR226DR",
        serial="A2K4-771-01",
        location="Weighing room 101",
        protocols=["rest", "labx", "astm"],
        tcp_enabled=True,
        panel="WEIGHING",
    ),
    Device(
        device_id="CHR-001",
        type="chromatograph_gc",
        name="Gas Chromatograph / Газовый хроматограф",
        manufacturer="Agilent",
        model="8890 GC",
        serial="GC-8890-4412",
        location="Chromatography lab 204",
        protocols=["rest", "astm", "fhir"],
        tcp_enabled=True,
        panel="GC-ASSAY",
    ),
    Device(
        device_id="CHR-002",
        type="chromatograph_hplc",
        name="HPLC Chromatograph / Жидкостный хроматограф",
        manufacturer="Shimadzu",
        model="LC-2050C",
        serial="LC2050-99381",
        location="Chromatography lab 204",
        protocols=["rest", "labx", "fhir"],
        panel="HPLC-ASSAY",
    ),
    Device(
        device_id="ANA-001",
        type="analyzer_chem",
        name="Clinical Chemistry Analyzer / Биохимический анализатор",
        manufacturer="Roche",
        model="cobas c311",
        serial="C311-2019-088",
        location="Clinical lab 102",
        protocols=["rest", "astm", "hl7v2"],
        tcp_enabled=True,
        panel="BIOCHEM",
    ),
    Device(
        device_id="ANA-002",
        type="analyzer_hema",
        name="Hematology Analyzer / Гематологический анализатор",
        manufacturer="Sysmex",
        model="XN-1000",
        serial="XN1000-55210",
        location="Clinical lab 102",
        protocols=["rest", "astm", "hl7v2", "fhir"],
        tcp_enabled=True,
        panel="HEMOGRAM",
    ),
    Device(
        device_id="PHM-001",
        type="ph_meter",
        name="pH Meter / pH-метр",
        manufacturer="Mettler Toledo",
        model="SevenExcellence S400",
        serial="S400-73012",
        location="QC lab 103",
        protocols=["rest", "labx"],
        panel="PH-TEST",
    ),
    Device(
        device_id="SPC-001",
        type="spectrophotometer",
        name="UV Spectrophotometer / Спектрофотометр",
        manufacturer="Shimadzu",
        model="UV-1900i",
        serial="UV1900-A4471",
        location="QC lab 103",
        protocols=["rest", "fhir", "labx"],
        panel="UV-SCAN",
    ),
]

def load_devices(path: str) -> list[Device]:
    """Read a device list from a JSON file: an array of objects with the Device fields."""
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit(f"MOCK_DEVICES_FILE: cannot read {path}: {e}")
    if not isinstance(items, list) or not items:
        raise SystemExit(f"MOCK_DEVICES_FILE: {path} must hold a non-empty JSON array")
    known = {f.name for f in fields(Device)}
    devices, seen = [], set()
    for i, item in enumerate(items):
        where = f"MOCK_DEVICES_FILE: {path}, device #{i + 1}"
        if not isinstance(item, dict):
            raise SystemExit(f"{where}: expected an object")
        unknown = set(item) - known
        if unknown:
            raise SystemExit(f"{where}: unknown fields {sorted(unknown)}")
        try:
            d = Device(**{**item, "protocols": list(item.get("protocols", []))})
        except TypeError as e:
            raise SystemExit(f"{where}: {e}")
        if d.type not in DEVICE_TYPES:
            raise SystemExit(f"{where}: type '{d.type}' is not one of {sorted(DEVICE_TYPES)}")
        if not set(d.protocols) <= PROTOCOLS:
            raise SystemExit(f"{where}: protocols {sorted(set(d.protocols) - PROTOCOLS)} "
                             f"are not among {sorted(PROTOCOLS)}")
        if d.device_id.upper() in seen:
            raise SystemExit(f"{where}: duplicate device_id '{d.device_id}'")
        seen.add(d.device_id.upper())
        devices.append(d)
    return devices


DEVICES: list[Device] = (load_devices(os.environ["MOCK_DEVICES_FILE"])
                         if os.environ.get("MOCK_DEVICES_FILE") else DEFAULT_DEVICES)
DEVICES_BY_ID: dict[str, Device] = {d.device_id.upper(): d for d in DEVICES}


def get_device(device_id: str) -> Device | None:
    return DEVICES_BY_ID.get(device_id.upper())


# ---------------------------------------------------------------------------
# Runtime settings from the environment
# ---------------------------------------------------------------------------

def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if not raw.strip().isdigit() or not 0 < int(raw) < 65536:
        raise SystemExit(f"{name}: '{raw}' is not a TCP port (1-65535)")
    return int(raw)


def parse_push_allowlist(raw: str | None) -> tuple[set[str], list] | None:
    """MOCK_PUSH_ALLOWED_HOSTS: comma-separated host names, IP addresses and CIDR
    networks the push helpers may connect to. Unset or "*" means any host
    (returned as None); an empty value allows none."""
    if raw is None or raw.strip() == "*":
        return None
    names, networks = set(), []
    for item in (x.strip() for x in raw.split(",")):
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            names.add(item.lower())
    return names, networks


# Raw ASTM / HL7 MLLP listeners started by main.py
ASTM_TCP_HOST = os.environ.get("MOCK_ASTM_HOST", "0.0.0.0")
ASTM_TCP_PORT = _env_port("MOCK_ASTM_PORT", 5150)
HL7_MLLP_HOST = os.environ.get("MOCK_HL7_HOST", "0.0.0.0")
HL7_MLLP_PORT = _env_port("MOCK_HL7_PORT", 2575)

LOG_LEVEL = os.environ.get("MOCK_LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR"):
    raise SystemExit(f"MOCK_LOG_LEVEL: '{LOG_LEVEL}' is not one of DEBUG, INFO, WARNING, ERROR")
PUSH_ALLOWED_HOSTS = parse_push_allowlist(os.environ.get("MOCK_PUSH_ALLOWED_HOSTS"))
