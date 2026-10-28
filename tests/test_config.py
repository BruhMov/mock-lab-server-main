"""Environment settings, the devices file and the push allowlist."""

import asyncio
import dataclasses
import ipaddress
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import config, simulator
from app.routers import astm as astm_router
from app.tcp import outbound

REPO = Path(__file__).resolve().parents[1]


def run_config(env: dict, expr: str) -> subprocess.CompletedProcess:
    """Import app.config in a fresh interpreter with `env` and print `expr`."""
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("MOCK_")} | env
    return subprocess.run([sys.executable, "-B", "-c", f"import app.config as c; print({expr})"],
                          cwd=REPO, env=full_env, capture_output=True, text=True)


def test_ports_and_hosts_from_env():
    out = run_config({"MOCK_ASTM_PORT": "15150", "MOCK_HL7_HOST": "127.0.0.1"},
                     "c.ASTM_TCP_PORT, c.HL7_MLLP_HOST, c.HL7_MLLP_PORT")
    assert out.stdout.split() == ["15150", "127.0.0.1", "2575"]


@pytest.mark.parametrize("env,message", [
    ({"MOCK_ASTM_PORT": "abc"}, "MOCK_ASTM_PORT"),
    ({"MOCK_HL7_PORT": "70000"}, "MOCK_HL7_PORT"),
    ({"MOCK_LOG_LEVEL": "LOUD"}, "MOCK_LOG_LEVEL"),
])
def test_invalid_settings_stop_with_a_message(env, message):
    out = run_config(env, "1")
    assert out.returncode != 0 and message in out.stderr


def test_devices_file_replaces_the_registry(tmp_path):
    f = tmp_path / "devices.json"
    f.write_text(json.dumps([{
        "device_id": "ana-009", "type": "analyzer_chem", "name": "Test analyzer",
        "manufacturer": "ACME", "model": "A1", "serial": "1", "location": "Lab",
        "protocols": ["rest", "astm"], "tcp_enabled": True, "panel": "BIOCHEM",
    }]), encoding="utf-8")
    out = run_config({"MOCK_DEVICES_FILE": str(f)},
                     "[d.device_id for d in c.DEVICES], c.get_device('ANA-009').model")
    assert out.stdout.strip() == "['ana-009'] A1"


@pytest.mark.parametrize("devices,message", [
    ([], "non-empty JSON array"),
    ([{"device_id": "X"}], "missing"),
    ([{"device_id": "X", "type": "toaster", "name": "", "manufacturer": "", "model": "",
       "serial": "", "location": ""}], "type 'toaster'"),
    ([{"device_id": "X", "type": "balance", "name": "", "manufacturer": "", "model": "",
       "serial": "", "location": "", "protocols": ["smtp"]}], "smtp"),
    ([{"device_id": "X", "colour": "red"}], "unknown fields"),
])
def test_invalid_devices_file_stops_with_a_message(tmp_path, devices, message):
    f = tmp_path / "devices.json"
    f.write_text(json.dumps(devices), encoding="utf-8")
    out = run_config({"MOCK_DEVICES_FILE": str(f)}, "1")
    assert out.returncode != 0 and message in out.stderr


def test_example_devices_file_matches_the_built_in_registry():
    assert config.load_devices(str(REPO / "devices.example.json")) == config.DEFAULT_DEVICES
    assert config.DEVICE_TYPES == set(simulator._GENERATORS)


def test_parse_push_allowlist():
    assert config.parse_push_allowlist(None) is None
    assert config.parse_push_allowlist("*") is None
    assert config.parse_push_allowlist("") == (set(), [])
    names, nets = config.parse_push_allowlist("LIMS.local, 10.0.0.0/8, 127.0.0.1")
    assert names == {"lims.local"}
    assert nets == [ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("127.0.0.1/32")]


async def _try_connect(allow: str | None):
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    old = config.PUSH_ALLOWED_HOSTS
    config.PUSH_ALLOWED_HOSTS = config.parse_push_allowlist(allow)
    try:
        _, writer = await outbound.open_push_connection("127.0.0.1", port, timeout=5)
        writer.close()
        return "connected"
    except outbound.PushNotAllowed:
        return "refused"
    finally:
        config.PUSH_ALLOWED_HOSTS = old
        server.close()


@pytest.mark.parametrize("allow,result", [
    (None, "connected"), ("127.0.0.0/8", "connected"), ("127.0.0.1", "connected"),
    ("10.0.0.0/8", "refused"), ("", "refused"), ("lims.local", "refused"),
])
def test_allowlist_controls_outbound_connections(allow, result):
    assert asyncio.run(_try_connect(allow)) == result


def test_push_outside_allowlist_is_forbidden(monkeypatch):
    monkeypatch.setattr(config, "PUSH_ALLOWED_HOSTS", config.parse_push_allowlist("10.0.0.0/8"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(astm_router.astm_push("ANA-001", "127.0.0.1", 9, sample_id=None))
    assert e.value.status_code == 403


def test_device_dataclass_round_trips_through_json():
    d = config.DEFAULT_DEVICES[0]
    assert config.Device(**json.loads(json.dumps(dataclasses.asdict(d)))) == d
