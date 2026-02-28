"""Govee LAN API controller — UDP unicast with optional multicast discovery."""

import json
import logging
import socket
import struct
import threading
from typing import Any

from .base import DeviceController

logger = logging.getLogger(__name__)

# Protocol constants
CONTROL_PORT = 4003
DISCOVERY_PORT = 4001
MULTICAST_ADDR = "239.255.255.250"


def _get_lan_ip() -> str:
    """Determine the local IP of the default-route interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def discover_govee(timeout: float = 5.0) -> dict:
    """Multicast scan on 239.255.255.250:4001.  Returns first device info dict.

    Binds to (lan_ip, 4001) with explicit IP_MULTICAST_IF to avoid
    WSL2 virtual NIC hijacking multicast.

    Returns:
        {"ip": str, "device": str, "sku": str, ...} from the scan response.

    Raises:
        TimeoutError if no device responds.
    """
    lan_ip = _get_lan_ip()
    logger.info("Discovery using LAN interface: %s", lan_ip)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        struct.pack("4s4s", socket.inet_aton(MULTICAST_ADDR), socket.inet_aton(lan_ip)),
    )
    sock.bind((lan_ip, DISCOVERY_PORT))
    sock.settimeout(timeout)

    scan = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
    sock.sendto(scan, (MULTICAST_ADDR, DISCOVERY_PORT))

    try:
        while True:
            data, addr = sock.recvfrom(4096)
            if addr[0] == lan_ip:
                continue
            msg = json.loads(data.decode()).get("msg", {})
            if msg.get("cmd") == "scan" and "device" in msg.get("data", {}):
                info = msg["data"]
                info.setdefault("ip", addr[0])
                return info
    except socket.timeout:
        raise TimeoutError("No Govee device responded to multicast scan")
    finally:
        sock.close()


class GoveeController(DeviceController):
    """Controls a single Govee light over the LAN API (UDP).

    Thread-safe: all socket sends go through a lock so the FastAPI
    async endpoints (which may run on different threads) won't interleave
    UDP datagrams.

    Usage:
        ctrl = GoveeController(ip="192.168.8.233")
        ctrl.connect()
        ctrl.set_color(255, 0, 0)
        ctrl.disconnect()

    If ip is None, connect() will attempt multicast discovery.
    """

    def __init__(self, ip: str | None = None, name: str = "Govee Light"):
        self._configured_ip = ip
        self._name = name
        self._ip: str | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._online = False
        self._last_color: tuple[int, int, int] | None = None
        self._last_brightness: int | None = None
        self._power: bool = False

    @property
    def device_type(self) -> str:
        return "govee_light"

    # ── lifecycle ──────────────────────────────────────────────────

    def connect(self) -> None:
        if self._configured_ip:
            self._ip = self._configured_ip
            logger.info("Govee unicast mode -> %s (discovery skipped)", self._ip)
        else:
            logger.info("Running Govee multicast discovery ...")
            info = discover_govee()
            self._ip = info["ip"]
            logger.info("Discovered Govee device at %s", self._ip)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._online = True

    def disconnect(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None
        self._online = False

    # ── commands ───────────────────────────────────────────────────

    def power_on(self) -> None:
        self._send_cmd("turn", {"value": 1})
        self._power = True

    def power_off(self) -> None:
        self._send_cmd("turn", {"value": 0})
        self._power = False

    def set_brightness(self, pct: int) -> None:
        pct = max(1, min(100, pct))
        self._send_cmd("brightness", {"value": pct})
        self._last_brightness = pct

    def set_color(self, r: int, g: int, b: int) -> None:
        r, g, b = _clamp(r), _clamp(g), _clamp(b)
        self._send_cmd("colorwc", {
            "color": {"r": r, "g": g, "b": b},
            "colorTemInKelvin": 0,
        })
        self._last_color = (r, g, b)

    def set_color_temp(self, kelvin: int) -> None:
        """Set white-temperature mode (2000-9000 K)."""
        kelvin = max(2000, min(9000, kelvin))
        self._send_cmd("colorwc", {
            "color": {"r": 0, "g": 0, "b": 0},
            "colorTemInKelvin": kelvin,
        })
        self._last_color = None

    # ── status ────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "online": self._online,
            "device_type": self.device_type,
            "name": self._name,
            "ip": self._ip,
            "power": self._power,
            "color": self._last_color,
            "brightness": self._last_brightness,
        }

    # ── internal ──────────────────────────────────────────────────

    def _send_cmd(self, cmd: str, data: dict) -> None:
        if not self._sock or not self._ip:
            raise RuntimeError("GoveeController is not connected — call connect() first")
        payload = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()
        with self._lock:
            self._sock.sendto(payload, (self._ip, CONTROL_PORT))


def _clamp(v: int) -> int:
    return max(0, min(255, v))
