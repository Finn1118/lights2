"""Controller for KAUF smart plugs running ESPHome firmware.

Communicates via ESPHome's built-in REST API over HTTP.
"""

import logging
from typing import Any

import requests

from .base import DeviceController

logger = logging.getLogger(__name__)

_TIMEOUT = 3  # seconds — fail fast on LAN


class KaufPlugController(DeviceController):
    """Simple HTTP controller for a KAUF plug (ESPHome REST API)."""

    def __init__(self, ip: str, entity_id: str = "kauf_plug", name: str = "KAUF Plug"):
        self._ip = ip
        self._entity_id = entity_id
        self._name = name
        self._online = False
        self._power = False

    # ── DeviceController interface ────────────────────────────

    def connect(self) -> None:
        """Probe the plug to confirm it's reachable."""
        try:
            self._fetch_state()
            logger.info("%s connected at %s", self._name, self._ip)
        except Exception as exc:
            logger.warning("%s unreachable at %s: %s", self._name, self._ip, exc)

    def disconnect(self) -> None:
        """No-op — stateless HTTP, nothing to tear down."""

    def power_on(self) -> None:
        self._post("turn_on")

    def power_off(self) -> None:
        self._post("turn_off")

    def status(self) -> dict[str, Any]:
        self._fetch_state()
        return {
            "online": self._online,
            "device_type": self.device_type,
            "name": self._name,
            "ip": self._ip,
            "power": self._power,
        }

    @property
    def device_type(self) -> str:
        return "kauf_plug"

    # ── Internal helpers ──────────────────────────────────────

    def _base_url(self) -> str:
        return f"http://{self._ip}/switch/{self._entity_id}"

    def _fetch_state(self) -> None:
        """GET current switch state from ESPHome."""
        try:
            resp = requests.get(self._base_url(), timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            self._online = True
            self._power = data.get("value", False)
        except Exception:
            self._online = False

    def _post(self, action: str) -> None:
        """POST a turn_on / turn_off / toggle action."""
        try:
            resp = requests.post(f"{self._base_url()}/{action}", timeout=_TIMEOUT)
            resp.raise_for_status()
            self._online = True
            self._power = action == "turn_on"
        except Exception as exc:
            self._online = False
            logger.error("%s %s failed: %s", self._name, action, exc)
            raise
