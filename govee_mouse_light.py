#!/usr/bin/env python3
"""
Govee LAN API — Mouse-to-Light Controller

UDP-only, direct-to-IP control at 20 Hz.
Mouse X → Hue, Mouse Y → Brightness.
Zero-transition color updates for ultra-low latency.

Requirements: pip install pynput
"""

import json
import socket
import struct
import time
import colorsys
import ctypes
from threading import Lock
from pynput import mouse

# ── Protocol constants ──────────────────────────────────────────────
MULTICAST_ADDR = "239.255.255.250"
DISCOVERY_PORT = 4001
CONTROL_PORT   = 4003
SEND_HZ        = 20
SEND_INTERVAL  = 1.0 / SEND_HZ

# ── Optional: set this to skip multicast discovery entirely ─────────
# Use if your router's IGMP snooping blocks the multicast scan response.
# Find the device IP in your router's client list (GL.iNet: 192.168.8.1).
DEVICE_IP = "192.168.8.233"  # e.g. "192.168.8.233"


# ── Network helpers ─────────────────────────────────────────────────

def get_lan_ip():
    """Return the local IP of the active LAN interface via routing table probe."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ── Discovery ───────────────────────────────────────────────────────

def discover_device(timeout=5.0):
    """Multicast scan on 239.255.255.250:4001. Returns (ip, device_id, sku).

    Uses a single socket bound to (lan_ip, 4001) so that:
      - IP_MULTICAST_IF forces the packet out on the physical LAN adapter,
        not the WSL virtual NIC (which has a lower routing metric on Windows).
      - The source IP in the UDP header is lan_ip, so the Govee knows where
        to send its unicast response.
      - IP_MULTICAST_LOOP=0 prevents the socket from receiving its own echo,
        which would cause a false-positive match on the scan response filter.
    """
    lan_ip = get_lan_ip()
    print(f"[discovery] Using LAN interface {lan_ip}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    # Force multicast out on the physical Ethernet NIC, not WSL/Wi-Fi
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(lan_ip))
    # Don't receive our own outgoing multicast
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    # Join the group so we can receive the Govee's response (sent to 239.255.255.250)
    mreq = struct.pack("4s4s",
                       socket.inet_aton(MULTICAST_ADDR),
                       socket.inet_aton(lan_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    # Bind to lan_ip:4001 — sets the UDP source address the Govee will reply to
    sock.bind((lan_ip, DISCOVERY_PORT))
    sock.settimeout(timeout)

    scan = json.dumps({
        "msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}
    }).encode()

    sock.sendto(scan, (MULTICAST_ADDR, DISCOVERY_PORT))
    print(f"[discovery] Scan sent to {MULTICAST_ADDR}:{DISCOVERY_PORT}")

    try:
        while True:
            data, addr = sock.recvfrom(4096)
            if addr[0] == lan_ip:
                continue  # discard any self-echo that slips through
            resp = json.loads(data.decode())
            msg = resp.get("msg", {})
            if msg.get("cmd") == "scan":
                d = msg["data"]
                ip = d.get("ip", addr[0])
                dev = d.get("device", "unknown")
                sku = d.get("sku", "unknown")
                print(f"[discovery] Found {sku} at {ip}  (id={dev})")
                return ip, dev, sku
    except socket.timeout:
        raise TimeoutError("No Govee device responded within timeout")
    finally:
        sock.close()


# ── Color mapping ───────────────────────────────────────────────────

def xy_to_rgb(x, y, w, h):
    """Map screen coordinates to RGB via HSV.
       X axis → Hue (full spectrum left-to-right)
       Y axis → Value/brightness (bright at top, dark at bottom)
    """
    hue = (x / w) % 1.0
    val = max(0.0, min(1.0, 1.0 - y / h))
    r, g, b = colorsys.hsv_to_rgb(hue, 1.0, val)
    return int(r * 255), int(g * 255), int(b * 255)


# ── Payload builders ────────────────────────────────────────────────

def cmd_turn_on():
    return json.dumps({
        "msg": {"cmd": "turn", "data": {"value": 1}}
    }).encode()

def cmd_brightness(val):
    return json.dumps({
        "msg": {"cmd": "brightness", "data": {"value": val}}
    }).encode()

def cmd_color(r, g, b):
    return json.dumps({
        "msg": {"cmd": "colorwc", "data": {
            "color": {"r": r, "g": g, "b": b},
            "colorTemInKelvin": 0
        }}
    }).encode()


# ── Main loop ───────────────────────────────────────────────────────

def main():
    # Detect screen resolution (Windows)
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()          # handle scaled displays
        scr_w = user32.GetSystemMetrics(0)
        scr_h = user32.GetSystemMetrics(1)
    except Exception:
        scr_w, scr_h = 1920, 1080
    print(f"[init] Screen {scr_w}x{scr_h}")

    # Discover light — skip if DEVICE_IP is hardcoded above
    if DEVICE_IP:
        print(f"[init] Using hardcoded device IP {DEVICE_IP} (discovery skipped)")
        device_ip = DEVICE_IP
    else:
        print("[init] Scanning LAN for Govee device...")
        try:
            device_ip, device_id, sku = discover_device()
        except TimeoutError:
            print()
            print("[error] Discovery timed out. Your router is likely dropping the")
            print("        multicast scan response (IGMP snooping on br-lan).")
            print()
            print("  QUICK FIX — set DEVICE_IP at the top of this script:")
            print(f"    DEVICE_IP = \"<your light's IP from router client list>\"")
            print()
            print("  PERMANENT FIX — SSH into GL.iNet Opal and run:")
            print("    echo 0 > /sys/devices/virtual/net/br-lan/bridge/multicast_snooping")
            print("    echo 0 > /sys/devices/virtual/net/br-lan/bridge/multicast_querier")
            raise SystemExit(1)

    # UDP control socket (non-blocking send)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (device_ip, CONTROL_PORT)

    # Turn on + max brightness
    sock.sendto(cmd_turn_on(), target)
    time.sleep(0.05)
    sock.sendto(cmd_brightness(100), target)
    time.sleep(0.05)

    # Shared mouse state — written at 1000 Hz by listener, read at 20 Hz by sender
    mx, my = scr_w // 2, scr_h // 2
    lock = Lock()

    def on_move(x, y):
        nonlocal mx, my
        with lock:
            mx, my = int(x), int(y)

    listener = mouse.Listener(on_move=on_move)
    listener.start()

    print(f"[run] Streaming to {device_ip}:{CONTROL_PORT} at {SEND_HZ} Hz")
    print("[run] Move mouse to change color.  Ctrl+C to quit.")

    prev_rgb = None
    packets_sent = 0
    t_start_stats = time.perf_counter()

    try:
        while True:
            t0 = time.perf_counter()

            # Read latest position
            with lock:
                cx, cy = mx, my

            rgb = xy_to_rgb(cx, cy, scr_w, scr_h)

            # Only send when color actually changes (saves bandwidth)
            if rgb != prev_rgb:
                sock.sendto(cmd_color(*rgb), target)
                prev_rgb = rgb
                packets_sent += 1

            # Stats every 5 seconds
            elapsed_stats = time.perf_counter() - t_start_stats
            if elapsed_stats >= 5.0:
                rate = packets_sent / elapsed_stats
                print(f"[stats] {rate:.1f} pkt/s  |  color=({rgb[0]:3d},{rgb[1]:3d},{rgb[2]:3d})")
                packets_sent = 0
                t_start_stats = time.perf_counter()

            # Precise 20 Hz sleep
            dt = time.perf_counter() - t0
            remaining = SEND_INTERVAL - dt
            if remaining > 0:
                time.sleep(remaining)

    except KeyboardInterrupt:
        print("\n[stop] Done.")
    finally:
        listener.stop()
        sock.close()


if __name__ == "__main__":
    main()
