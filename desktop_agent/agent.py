#!/usr/bin/env python3
"""Desktop Agent — streams mouse/screen data to the HomeControl API.

This runs on your PC and sends colour commands to the web backend,
keeping PC-specific dependencies (pynput, mss, ctypes/DPI) isolated
from the server.

Modes (keyboard shortcuts, always active):
    A   -> AMBIENT   Sample pixel under cursor, send to API
    F   -> FROZEN    Latch current colour, stop sending
    Q   -> Quit

Usage:
    python -m desktop_agent.agent                        # defaults
    python -m desktop_agent.agent --api http://10.0.0.5:8000  # remote server
    python -m desktop_agent.agent --hz 30                # 30 Hz update rate
"""

import argparse
import colorsys
import ctypes
import math
import sys
import threading
import time

import requests
from pynput import keyboard as pkeyboard
from pynput import mouse as pmouse


# ── Screen sampler (mss preferred, GDI fallback) ─────────────────

class ScreenSampler:
    """Grab a single pixel from the composited desktop frame buffer."""

    def __init__(self):
        try:
            import mss as _mss
            self._sct = _mss.mss()
            self._impl = "mss"
        except ImportError:
            self._gdi32 = ctypes.windll.gdi32
            self._user32 = ctypes.windll.user32
            self._dc = self._user32.GetDC(0)
            self._impl = "gdi"
            print("[sampler] mss not found — using GDI (pip install mss for better results)")

    def sample(self, x: int, y: int) -> tuple[int, int, int]:
        if self._impl == "mss":
            frame = self._sct.grab({"left": x, "top": y, "width": 1, "height": 1})
            b, g, r = frame.raw[0], frame.raw[1], frame.raw[2]
            return int(r), int(g), int(b)
        else:
            c = self._gdi32.GetPixel(self._dc, x, y)
            if c == -1:
                return 128, 128, 128
            return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF

    def close(self):
        if self._impl == "mss":
            self._sct.close()
        else:
            self._user32.ReleaseDC(0, self._dc)


# ── Agent ─────────────────────────────────────────────────────────

class DesktopAgent:
    """Streams mouse position / ambient colour to the HomeControl API."""

    def __init__(self, api_base: str, send_hz: int = 20):
        self._api = api_base.rstrip("/")
        self._interval = 1.0 / send_hz
        self._sampler = ScreenSampler()
        self._running = True

        # Mouse position (written by pynput thread, read by main loop)
        self._mx = 0
        self._my = 0

        # Mode
        self._mode = "AMBIENT"
        self._prev_rgb: tuple[int, int, int] | None = None

        # DPI
        self._dpi_scale = 1.0
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            self._dpi_scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            pass

    def run(self):
        print(f"[agent] API target: {self._api}")
        print(f"[agent] DPI scale: {self._dpi_scale:.2f}x")
        print(f"[agent] Update rate: {1.0 / self._interval:.0f} Hz")
        print("[agent] A = ambient  |  F = freeze  |  Q = quit")

        self._start_listeners()

        try:
            while self._running:
                t0 = time.perf_counter()
                self._tick()
                elapsed = time.perf_counter() - t0
                sleep_for = self._interval - elapsed
                if sleep_for > 0:
                    time.sleep(sleep_for)
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()

    def _tick(self):
        if self._mode == "FROZEN":
            return

        px = int(self._mx * self._dpi_scale)
        py = int(self._my * self._dpi_scale)
        rgb = self._sampler.sample(px, py)

        if rgb == self._prev_rgb:
            return

        self._prev_rgb = rgb
        r, g, b = rgb
        try:
            requests.post(
                f"{self._api}/govee/color",
                json={"r": r, "g": g, "b": b},
                timeout=0.1,
            )
        except requests.RequestException:
            pass  # fire-and-forget; server might be restarting

    def _start_listeners(self):
        def on_move(x, y):
            self._mx = int(x)
            self._my = int(y)

        def on_press(key):
            try:
                ch = key.char
            except AttributeError:
                ch = None

            if ch == "a":
                self._mode = "AMBIENT"
                print("[mode] AMBIENT")
            elif ch == "f":
                if self._mode == "FROZEN":
                    self._mode = "AMBIENT"
                    print("[mode] AMBIENT (unfrozen)")
                else:
                    self._mode = "FROZEN"
                    print("[mode] FROZEN")
            elif ch == "q":
                self._running = False

        self._ml = pmouse.Listener(on_move=on_move)
        self._kl = pkeyboard.Listener(on_press=on_press)
        self._ml.daemon = self._kl.daemon = True
        self._ml.start()
        self._kl.start()

    def _cleanup(self):
        print("\n[agent] shutting down")
        self._ml.stop()
        self._kl.stop()
        self._sampler.close()


# ── CLI entry point ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Desktop Agent for HomeControl")
    parser.add_argument(
        "--api",
        default="http://127.0.0.1:8000",
        help="HomeControl API base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--hz",
        type=int,
        default=20,
        help="Colour update rate in Hz (default: 20)",
    )
    args = parser.parse_args()

    agent = DesktopAgent(api_base=args.api, send_hz=args.hz)
    agent.run()


if __name__ == "__main__":
    main()
