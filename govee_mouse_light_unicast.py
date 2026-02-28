#!/usr/bin/env python3
"""
Govee LAN API — Mouse-to-Light Controller (Unicast / No-Discovery Mode)

Use this when multicast discovery fails due to router IGMP snooping.
Set DEVICE_IP to the IP shown in your router's client list.
All other behaviour is identical to govee_mouse_light.py.
"""

import json
import socket
import time
import colorsys
import ctypes
from threading import Lock
from pynput import mouse

# ── REQUIRED: Set this to your light's IP from the router client list ─
DEVICE_IP = None  # e.g. "192.168.8.105"
# ─────────────────────────────────────────────────────────────────────

CONTROL_PORT  = 4003
SEND_HZ       = 20
SEND_INTERVAL = 1.0 / SEND_HZ


def xy_to_rgb(x, y, w, h):
    hue = (x / w) % 1.0
    val = max(0.0, min(1.0, 1.0 - y / h))
    r, g, b = colorsys.hsv_to_rgb(hue, 1.0, val)
    return int(r * 255), int(g * 255), int(b * 255)

def cmd_color(r, g, b):
    return json.dumps({
        "msg": {"cmd": "colorwc", "data": {
            "color": {"r": r, "g": g, "b": b},
            "colorTemInKelvin": 0
        }}
    }).encode()


def main():
    if not DEVICE_IP:
        raise SystemExit("ERROR: Set DEVICE_IP at the top of this script.")

    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        scr_w = user32.GetSystemMetrics(0)
        scr_h = user32.GetSystemMetrics(1)
    except Exception:
        scr_w, scr_h = 1920, 1080

    print(f"[init] Screen {scr_w}x{scr_h}")
    print(f"[init] Target {DEVICE_IP}:{CONTROL_PORT} (unicast, no discovery)")

    sock   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (DEVICE_IP, CONTROL_PORT)

    sock.sendto(json.dumps({"msg": {"cmd": "turn",       "data": {"value": 1  }}}).encode(), target)
    time.sleep(0.05)
    sock.sendto(json.dumps({"msg": {"cmd": "brightness", "data": {"value": 100}}}).encode(), target)
    time.sleep(0.05)

    mx, my = scr_w // 2, scr_h // 2
    lock   = Lock()

    def on_move(x, y):
        nonlocal mx, my
        with lock:
            mx, my = int(x), int(y)

    listener = mouse.Listener(on_move=on_move)
    listener.start()

    print(f"[run] Streaming at {SEND_HZ} Hz. Move mouse to change color. Ctrl+C to quit.")

    prev_rgb = None
    try:
        while True:
            t0 = time.perf_counter()
            with lock:
                cx, cy = mx, my
            rgb = xy_to_rgb(cx, cy, scr_w, scr_h)
            if rgb != prev_rgb:
                sock.sendto(cmd_color(*rgb), target)
                prev_rgb = rgb
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
