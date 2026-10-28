#!/usr/bin/env bash
# Start the mock laboratory equipment server.
# HTTP REST + Swagger UI : MOCK_HTTP_HOST:MOCK_HTTP_PORT, default 0.0.0.0:8000 (docs at /docs)
# Raw ASTM TCP listener  : MOCK_ASTM_HOST:MOCK_ASTM_PORT, default 0.0.0.0:5150
# HL7 MLLP TCP listener  : MOCK_HL7_HOST:MOCK_HL7_PORT, default 0.0.0.0:2575
# MOCK_RELOAD=1 restarts the server on code changes (development only).
# Other settings (devices file, log level, push allowlist): see README, "Configuration".
cd "$(dirname "$0")"
reload=()
[ "${MOCK_RELOAD:-0}" = "1" ] && reload=(--reload)
exec python3 -m uvicorn app.main:app \
    --host "${MOCK_HTTP_HOST:-0.0.0.0}" --port "${MOCK_HTTP_PORT:-8000}" "${reload[@]}"
