"""Random measurement generator for every simulated equipment type.

`generate_measurement()` is the single entry point used by all protocol layers
(REST, ASTM, HL7v2, FHIR, LabX) so the same sample yields a coherent result set
regardless of the transport the LIMS chooses to talk to.
"""

from __future__ import annotations

import random
import string
from datetime import datetime

from .config import Device

_rng = random.Random()


def random_sample_id(prefix: str = "S") -> str:
    """e.g. S-2026-84213"""
    return f"{prefix}-{datetime.now().year}-{_rng.randint(10000, 99999)}"


def random_operator() -> str:
    return _rng.choice(["ivanov_i", "petrova_a", "smirnov_s", "kuznetsov_d", "operator1"])


def now_ts(fmt: str = "%Y%m%d%H%M%S") -> str:
    return datetime.now().strftime(fmt)


def _flag(low: float, high: float, out_of_range_chance: float = 0.15) -> tuple[float, str]:
    """Random value in [low, high]; ~15% of results are pushed outside and flagged H/L."""
    r = _rng.random()
    if r < out_of_range_chance / 2:
        value = low * _rng.uniform(0.6, 0.95)
        return round(value, 3), "L"
    if r < out_of_range_chance:
        value = high * _rng.uniform(1.05, 1.4)
        return round(value, 3), "H"
    value = _rng.uniform(low, high)
    return round(value, 3), ""


# ---------------------------------------------------------------------------
# Per-type generators. Each returns a dict:
#   {"panel": str, "unit_system": str, "values": [ {...}, ... ], "extra": {...}}
# A "value" dict always carries: code, name, value, unit and optional
# ref_range / flag — this mirrors the OBX / R-record shape of HL7 / ASTM.
# ---------------------------------------------------------------------------

def _balance(device: Device, sample_id: str) -> dict:
    target_g = _rng.choice([0.5, 1.0, 2.0, 5.0, 10.0, 50.0])
    tare = round(_rng.uniform(0.9, 1.1), 4) if _rng.random() < 0.5 else 0.0
    gross = round(tare + target_g + _rng.uniform(-0.0008, 0.0008), 4)
    net = round(gross - tare, 4)
    return {
        "panel": "WEIGHING",
        "values": [
            {"code": "GROSS", "name": "Gross weight", "value": gross, "unit": "g",
             "ref_range": None, "flag": "", "decimals": 4},
            {"code": "TARE", "name": "Tare weight", "value": tare, "unit": "g",
             "ref_range": None, "flag": "", "decimals": 4},
            {"code": "NET", "name": "Net weight", "value": net, "unit": "g",
             "ref_range": None, "flag": "", "decimals": 4},
        ],
        "extra": {
            "stability": _rng.choice(["STABLE", "STABLE", "STABLE", "DRIFTING"]),
            "readability_mg": 0.1,
            "temperature_c": round(_rng.uniform(19.5, 22.5), 1),
        },
    }


def _chromatograph(device: Device, sample_id: str) -> dict:
    is_gc = device.type == "chromatograph_gc"
    method = "USP-METHANOL-GC" if is_gc else "USP-HPLC-042"
    n_peaks = _rng.randint(3, 7)
    main_idx = _rng.randrange(n_peaks)
    compounds = (
        ["Ethanol", "Methanol", "Isopropanol", "Acetone", "Ethyl acetate", "Toluene"]
        if is_gc
        else ["Caffeine", "Paracetamol", "Ascorbic acid", "Benzoic acid", "Impurity-A", "Impurity-B"]
    )
    peaks = []
    rt = _rng.uniform(2.0, 4.0)
    for i in range(n_peaks):
        rt += round(_rng.uniform(0.8, 3.5), 3)
        area = _rng.lognormvariate(6 if i == main_idx else 4.2, 0.4)
        height = round(area / _rng.uniform(8, 20), 1)
        peaks.append({
            "peak_no": i + 1,
            "rt_min": round(rt, 3),
            "area": round(area, 1),
            "area_percent": None,      # filled below
            "height": height,
            "width_min": round(_rng.uniform(0.05, 0.3), 3),
            "tailing_factor": round(_rng.uniform(0.9, 1.6), 2),
            "resolution": round(_rng.uniform(1.5, 12.0), 2) if i else None,
            "plates": _rng.randint(5000, 120000),
            "compound": compounds[i % len(compounds)],
        })
    total = sum(p["area"] for p in peaks)
    for p in peaks:
        p["area_percent"] = round(p["area"] / total * 100, 2)
    main = peaks[main_idx]
    r = _rng.random()  # ~15% of assays are out of specification (half low, half high)
    if r < 0.075:
        assay = round(_rng.uniform(90.0, 94.9), 2)
    elif r < 0.15:
        assay = round(_rng.uniform(105.1, 110.0), 2)
    else:
        assay = round(_rng.uniform(95.0, 105.0), 2)
    values = [
        {"code": "ASSAY", "name": "Assay (main peak)", "value": assay, "unit": "%",
         "ref_range": "95.0-105.0", "flag": "L" if assay < 95 else "H" if assay > 105 else "",
         "decimals": 2},
        {"code": "RT", "name": "Main peak retention time", "value": main["rt_min"],
         "unit": "min", "ref_range": None, "flag": "", "decimals": 3},
        {"code": "PURITY", "name": "Peak purity angle", "value": round(_rng.uniform(0.1, 0.9), 3),
         "unit": "deg", "ref_range": None, "flag": "", "decimals": 3},
    ]
    return {
        "panel": method,
        "values": values,
        "extra": {
            "peaks": peaks,
            "column": "DB-5ms 30m" if is_gc else "C18 150x4.6mm 5um",
            "carrier_or_mobile_phase": "He 1.2 mL/min" if is_gc else "ACN/water 60/40",
            "injection_ul": _rng.choice([1, 5, 10, 20]),
            "column_temp_c": round(_rng.uniform(30.0, 250.0), 1) if is_gc else round(_rng.uniform(25.0, 40.0), 1),
            "detection_nm": None if is_gc else round(_rng.choice([210, 220, 254, 280])),
            "chromatogram": _synth_chromatogram(peaks),
        },
    }


def _synth_chromatogram(peaks: list[dict], n_points: int = 200) -> list[list[float]]:
    """Synthesize a simple signal trace (time_min, mAU) around the peaks."""
    t_end = max(p["rt_min"] for p in peaks) + 1.0
    pts = []
    for i in range(n_points):
        t = t_end * i / (n_points - 1)
        y = _rng.uniform(0.2, 1.2)
        for p in peaks:
            w = max(p["width_min"], 0.06)
            y += (p["height"] / 40.0) * pow(2.718, -((t - p["rt_min"]) ** 2) / (2 * (w / 2.355) ** 2))
        pts.append([round(t, 3), round(y, 3)])
    return pts


CHEM_PANEL = [
    ("GLU", "Glucose", "mmol/L", 3.9, 6.4),
    ("ALT", "Alanine aminotransferase", "U/L", 7.0, 56.0),
    ("AST", "Aspartate aminotransferase", "U/L", 10.0, 40.0),
    ("CREA", "Creatinine", "umol/L", 62.0, 115.0),
    ("CHOL", "Cholesterol", "mmol/L", 3.0, 5.2),
    ("ALB", "Albumin", "g/L", 35.0, 52.0),
]

HEMA_PANEL = [
    ("WBC", "White blood cell count", "10^9/L", 4.0, 10.0),
    ("RBC", "Red blood cell count", "10^12/L", 4.2, 5.7),
    ("HGB", "Hemoglobin", "g/L", 130.0, 170.0),
    ("PLT", "Platelet count", "10^9/L", 150.0, 400.0),
    ("HCT", "Hematocrit", "%", 39.0, 49.0),
]


def _analyzer(device: Device, sample_id: str) -> dict:
    panel = CHEM_PANEL if device.type == "analyzer_chem" else HEMA_PANEL
    values = []
    for code, name, unit, lo, hi in panel:
        v, flag = _flag(lo, hi)
        values.append({
            "code": code, "name": name, "value": v, "unit": unit,
            "ref_range": f"{lo}-{hi}", "flag": flag, "decimals": 2,
        })
    return {
        "panel": "BIOCHEM" if device.type == "analyzer_chem" else "HEMOGRAM",
        "values": values,
        "extra": {
            "sample_type": "SERUM" if device.type == "analyzer_chem" else "EDTA WHOLE BLOOD",
            "dilution": 1,
            "reagent_lot": f"R{_rng.randint(400000, 499999)}",
            "qc_status": _rng.choice(["PASS", "PASS", "PASS", "WARN"]),
        },
    }


def _ph_meter(device: Device, sample_id: str) -> dict:
    ph, flag = _flag(4.5, 7.2, out_of_range_chance=0.2)
    ph = round(ph, 3)
    values = [
        {"code": "PH", "name": "pH value", "value": ph, "unit": "pH",
         "ref_range": "4.5-7.2", "flag": flag, "decimals": 3},
        {"code": "TEMP", "name": "Sample temperature", "value": round(_rng.uniform(19.0, 25.0), 1),
         "unit": "degC", "ref_range": None, "flag": "", "decimals": 1},
        {"code": "MVAL", "name": "mV reading", "value": round(_rng.uniform(-200, 200), 1),
         "unit": "mV", "ref_range": None, "flag": "", "decimals": 1},
    ]
    return {
        "panel": "PH-TEST",
        "values": values,
        "extra": {
            "calibration": [
                {"buffer_ph": 4.01, "slope_percent": round(_rng.uniform(95, 101), 1)},
                {"buffer_ph": 7.00, "slope_percent": round(_rng.uniform(95, 101), 1)},
                {"buffer_ph": 9.21, "slope_percent": round(_rng.uniform(95, 101), 1)},
            ],
            "electrode": "InLab Expert Pro-ISM",
        },
    }


def _spectrophotometer(device: Device, sample_id: str) -> dict:
    lam_max = _rng.choice([254, 260, 280, 540])
    if _rng.random() < 0.1:  # ~10% of readings exceed the 2.0 A linear range
        absorbance = round(_rng.uniform(2.05, 3.0), 4)
    else:
        absorbance = round(_rng.uniform(0.05, 1.8), 4)
    conc = round(absorbance * _rng.uniform(9.5, 10.5), 3)  # Beer-Lambert toy model
    values = [
        {"code": "ABS", "name": f"Absorbance @ {lam_max} nm", "value": absorbance,
         "unit": "A", "ref_range": "0.0-2.0", "flag": "" if absorbance <= 2.0 else "H", "decimals": 4},
        {"code": "CONC", "name": "Calculated concentration", "value": conc,
         "unit": "mg/L", "ref_range": None, "flag": "", "decimals": 3},
    ]
    scan = []
    for nm in range(220, 701, 10):
        a = round(absorbance * pow(2.718, -((nm - lam_max) ** 2) / (2 * 25.0 ** 2)) + _rng.uniform(0, 0.01), 4)
        scan.append([nm, a])
    return {
        "panel": "UV-SCAN",
        "values": values,
        "extra": {
            "lambda_max_nm": lam_max,
            "cuvette": "10 mm quartz",
            "bandwidth_nm": 1.0,
            "scan": scan,
        },
    }


_GENERATORS = {
    "balance": _balance,
    "chromatograph_gc": _chromatograph,
    "chromatograph_hplc": _chromatograph,
    "analyzer_chem": _analyzer,
    "analyzer_hema": _analyzer,
    "ph_meter": _ph_meter,
    "spectrophotometer": _spectrophotometer,
}


def generate_measurement(device: Device, sample_id: str | None = None) -> dict:
    """Generate one complete, random measurement for `device`."""
    sample_id = sample_id or random_sample_id()
    gen = _GENERATORS.get(device.type)
    if gen is None:
        raise ValueError(f"No simulator for device type '{device.type}'")
    payload = gen(device, sample_id)
    return {
        "sample_id": sample_id,
        "device_id": device.device_id,
        "device_type": device.type,
        "operator": random_operator(),
        "measured_at": now_ts("%Y-%m-%dT%H:%M:%S"),
        "status": "F",  # ASTM/HL7 final result flag
        **payload,
    }
