"""Samsung TV controller stub for UN43TU7000FXZA (2020 Tizen, WebSocket API).

The UN43TU7000 supports the Samsung SmartTV WebSocket protocol on port 8002
(wss://) for remote-control key simulation and basic queries.  The popular
`samsungtvws` Python library wraps this.

This stub defines the interface and wiring; actual WebSocket calls are marked
with TODO so you can flesh them out once the TV is on the network.

Install:  pip install samsungtvws[async]
"""

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .base import DeviceController

logger = logging.getLogger(__name__)

# Persists the pairing token so the TV's allow/deny popup only appears once.
# Stored next to this file; delete tv_token.txt to force a fresh pairing.
_TOKEN_FILE = Path(__file__).parent / "tv_token.txt"


class SamsungTVController(DeviceController):
    """Control a Samsung Tizen TV over the local-network WebSocket API.

    Args:
        ip: TV IP address (check your router's DHCP leases).
        port: WebSocket port — 8002 for wss, 8001 for ws.
        name: Friendly name shown on the TV's "allow/deny" popup.
    """

    def __init__(self, ip: str, port: int = 8002, name: str = "HomeControl",
                 mac: str | None = None):
        self._ip = ip
        self._port = port
        self._name = name
        self._mac = mac
        self._online = False
        self._power = False
        self._tv = None  # will hold SamsungTVWS instance
        self._repeat_active = False
        self._repeat_thread: threading.Thread | None = None

    @property
    def device_type(self) -> str:
        return "samsung_tv"

    # ── lifecycle ──────────────────────────────────────────────────

    def connect(self) -> None:
        """Open a WebSocket connection to the TV.

        First connection triggers an on-screen allow/deny prompt on the TV.
        After approval, the token is cached by samsungtvws.
        """
        try:
            from samsungtvws import SamsungTVWS
        except ImportError:
            logger.warning(
                "samsungtvws not installed — Samsung TV control unavailable. "
                "Install with: pip install samsungtvws[async]"
            )
            return

        self._tv = SamsungTVWS(
            host=self._ip,
            port=self._port,
            name=self._name,
            token_file=str(_TOKEN_FILE),
            timeout=3,          # fail fast when TV is off; default can be ~20 s
        )
        self._online = True
        logger.info("Samsung TV connected at %s:%d (token file: %s)",
                    self._ip, self._port, _TOKEN_FILE)

    def disconnect(self) -> None:
        if self._tv:
            try:
                self._tv.close()
            except Exception:
                pass
            self._tv = None
        self._online = False

    # ── power ─────────────────────────────────────────────────────

    def power_on(self) -> None:
        """Wake the TV via Wake-on-LAN magic packet.

        Sends a WoL magic packet to self._mac (if configured), then waits
        ~3 s for the TV to boot before any subsequent WebSocket commands are
        sent.  Falls back to KEY_POWER if no MAC is available.
        """
        if self._mac:
            try:
                import wakeonlan
                wakeonlan.send_magic_packet(self._mac)
                logger.info("WoL magic packet sent to %s — waiting 3 s for boot", self._mac)
                time.sleep(3)
            except ImportError:
                logger.warning(
                    "wakeonlan not installed — falling back to KEY_POWER. "
                    "Install with: pip install wakeonlan"
                )
                self._send_key("KEY_POWER")
        else:
            logger.warning("No MAC configured — sending KEY_POWER (may fail if TV is off)")
            self._send_key("KEY_POWER")
        self._power = True

    def power_off(self) -> None:
        self._send_key("KEY_POWER")
        self._power = False

    # ── TV-specific commands ──────────────────────────────────────

    def send_key(self, key: str) -> None:
        """Send an arbitrary remote-control key (e.g. KEY_VOLUP, KEY_MUTE)."""
        self._send_key(key)

    def set_volume(self, level: int) -> None:
        """Set volume to an absolute level (0-100).

        The WS API doesn't have a direct set-volume; this is a stub for a
        future implementation that could query current vol and send
        KEY_VOLUP/KEY_VOLDOWN accordingly, or use the SmartThings REST API.
        """
        # TODO: implement via SmartThings API or repeated key presses
        logger.info("set_volume(%d) — not yet implemented", level)

    def launch_app(self, app_id: str) -> None:
        """Launch a smart TV app by its app ID."""
        if not self._tv:
            logger.warning("TV not connected")
            return
        # TODO: self._tv.run_app(app_id)
        logger.info("launch_app(%s) — not yet implemented", app_id)

    def set_source(self, source: str) -> None:
        """Switch input source (HDMI1, HDMI2, TV, etc.)."""
        source_keys = {
            "TV": "KEY_TV",
            "HDMI1": "KEY_HDMI1",
            "HDMI2": "KEY_HDMI2",
            "HDMI3": "KEY_HDMI3",
            "HDMI4": "KEY_HDMI4",
        }
        key = source_keys.get(source.upper())
        if key:
            self._send_key(key)
        else:
            logger.warning("Unknown source: %s", source)

    # ── repeat (hold-to-repeat) ───────────────────────────────────

    def start_repeat(self, key: str) -> None:
        """Begin sending *key* every 75 ms until stop_repeat() is called.

        Calling start_repeat while a repeat is already running first stops
        the previous repeat, then starts a fresh one for the new key.
        """
        self.stop_repeat()
        self._repeat_active = True

        def _loop() -> None:
            while self._repeat_active:
                self._send_key(key)
                time.sleep(0.075)

        self._repeat_thread = threading.Thread(target=_loop, daemon=True)
        self._repeat_thread.start()
        logger.debug("Repeat started: %s", key)

    def stop_repeat(self) -> None:
        """Stop any in-progress key repeat immediately."""
        self._repeat_active = False
        if self._repeat_thread and self._repeat_thread.is_alive():
            self._repeat_thread.join(timeout=0.2)
        self._repeat_thread = None
        logger.debug("Repeat stopped")

    # ── status ────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "online": self._online,
            "device_type": self.device_type,
            "name": self._name,
            "ip": self._ip,
            "power": self._power,
        }

    # ── internal ──────────────────────────────────────────────────

    def _send_key(self, key: str) -> None:
        if not self._tv:
            logger.warning("TV not connected — ignoring key %s", key)
            return
        try:
            self._tv.send_key(key)
            logger.debug("Sent key: %s", key)
        except Exception as exc:
            # WinError 10060 (WSAETIMEDOUT) and ConnectionRefusedError both
            # surface as ConnectionError / TimeoutError subclasses of OSError.
            # When they occur and we have a MAC, the TV is most likely asleep —
            # fire a WoL magic packet first so the retry has a warm target.
            is_offline_error = isinstance(exc, (ConnectionError, TimeoutError))
            if is_offline_error and self._mac:
                logger.info(
                    "TV appears offline while sending key %s (%s) — "
                    "sending WoL magic packet before retry",
                    key, exc,
                )
                self.power_on()   # sends magic packet + sleeps 3 s
            else:
                logger.warning(
                    "Connection dropped sending key %s (%s) — reconnecting", key, exc
                )
            self._online = False
            try:
                self.connect()
                self._tv.send_key(key)
                logger.debug("Retry succeeded: %s", key)
            except Exception as retry_exc:
                logger.error("Retry failed for key %s: %s", key, retry_exc)
                self._online = False
