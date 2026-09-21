"""Runtime paths shared by the circuit tools."""

from __future__ import annotations

import os
from pathlib import Path

API_SOCKET_PATH = Path("/tmp/circuit-kicad.sock")
KONNECT_SOCKET_URL = f"ipc://{API_SOCKET_PATH}"
STATE_FILE = Path(os.environ.get("CIRCUIT_STATE_DIR", "/tmp/circuit")) / "api-server.json"
