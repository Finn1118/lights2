#!/usr/bin/env python3
"""
Govee LAN Controller — Colour Wheel GUI
========================================

Separated into three independent layers:
  GoveeUDP      — Network / protocol  (fire-and-forget UDP, no state)
  ScreenSampler — OS pixel sampling   (ambient mode, mss / GDI fallback)
  ColorWheelApp — GUI + 20 Hz loop    (tkinter mainloop owns everything)

Modes  (keyboard shortcuts always active):
  W      → WHEEL    Move cursor over the GUI window → colour from wheel geometry
  A      → AMBIENT  Cursor anywhere on screen       → sample pixel under cursor
  Space  → FROZEN   Latch current colour; Space again to resume

The colour wheel window also auto-switches:
  <Enter>  → activates WHEEL mode
  <Leave>  → restores previous mode

Requirements:
  pip install pynput Pillow mss
"""

import colorsys
import ctypes
import json
import math
import socket
import struct
import tkinter as tk

from pynput import keyboard as pkeyboard
from pynput import mouse as pmouse

try:
    from PIL import Image, ImageTk
except ImportError:
    raise SystemExit("Pillow is required:  pip install Pillow")


# ═══════════════════════════════════════════════════════════════════
#  CONFIG  ── edit these two lines
# ═══════════════════════════════════════════════════════════════════

DEVICE_IP = "192.168.8.233"   # None → use multicast discovery
SEND_HZ   = 20                # colour update rate (Hz)

# ── Wheel appearance ────────────────────────────────────────────────
WHEEL_SIZE = 280              # wheel diameter in pixels
BRIGHTNESS = 1.0              # HSV value component (0.0 – 1.0)
WIN_PAD    = 14               # window padding around wheel

# ── Protocol ────────────────────────────────────────────────────────
CONTROL_PORT   = 4003
DISCOVERY_PORT = 4001
MULTICAST_ADDR = "239.255.255.250"


# ═══════════════════════════════════════════════════════════════════
#  LAYER 1 — NETWORK / PROTOCOL
# ═══════════════════════════════════════════════════════════════════

class GoveeUDP:
    """
    Thin, stateless UDP wrapper around the Govee LAN API.

    Every method is fire-and-forget — Govee does not ACK colour
    or brightness commands, so we never block waiting for a reply.
    """

    def __init__(self, ip: str, port: int = CONTROL_PORT):
        self._target = (ip, port)
        self._sock   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # ── public API ─────────────────────────────────────────────────

    def turn_on(self) -> None:
        self._cmd("turn", {"value": 1})

    def set_brightness(self, pct: int) -> None:
        self._cmd("brightness", {"value": max(1, min(100, pct))})

    def set_color(self, r: int, g: int, b: int) -> None:
        self._cmd("colorwc", {
            "color": {"r": r, "g": g, "b": b},
            "colorTemInKelvin": 0,   # 0 = use RGB, not white-temperature
        })

    def close(self) -> None:
        self._sock.close()

    # ── internal ───────────────────────────────────────────────────

    def _cmd(self, cmd: str, data: dict) -> None:
        payload = json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()
        self._sock.sendto(payload, self._target)


def _get_lan_ip() -> str:
    """Determine the local IP of the default-route interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def discover_device(timeout: float = 5.0) -> str:
    """
    Multicast scan on 239.255.255.250:4001.  Returns first device IP.

    Binds to (lan_ip, 4001) so:
      - IP_MULTICAST_IF forces the packet out on Ethernet, not the WSL NIC.
      - Source IP in the UDP header is lan_ip → Govee responds to the right host.
      - IP_MULTICAST_LOOP=0 suppresses self-echo.
    """
    lan_ip = _get_lan_ip()
    print(f"[discovery] LAN interface: {lan_ip}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,  2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,   socket.inet_aton(lan_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    struct.pack("4s4s",
                                socket.inet_aton(MULTICAST_ADDR),
                                socket.inet_aton(lan_ip)))
    sock.bind((lan_ip, DISCOVERY_PORT))
    sock.settimeout(timeout)

    scan = json.dumps({
        "msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}
    }).encode()
    sock.sendto(scan, (MULTICAST_ADDR, DISCOVERY_PORT))

    try:
        while True:
            data, addr = sock.recvfrom(4096)
            if addr[0] == lan_ip:
                continue
            msg = json.loads(data.decode()).get("msg", {})
            if msg.get("cmd") == "scan" and "device" in msg.get("data", {}):
                return msg["data"].get("ip", addr[0])
    except socket.timeout:
        raise TimeoutError("No Govee device responded to multicast scan")
    finally:
        sock.close()


# ═══════════════════════════════════════════════════════════════════
#  LAYER 2 — SCREEN SAMPLER  (ambient mode only)
# ═══════════════════════════════════════════════════════════════════

class ScreenSampler:
    """
    Samples a single pixel from the composited screen frame buffer.

    Strategy A — mss  (preferred)
      Uses BitBlt against the DWM back buffer.
      Captures DirectX, OpenGL, video decode, and browser GPU layers.
      Requires:  pip install mss

    Strategy B — GDI GetPixel  (fallback, no extra install)
      Reads from the software GDI framebuffer.
      Misses hardware-accelerated content (games, browsers, video).
    """

    def __init__(self):
        try:
            import mss as _mss
            self._sct  = _mss.mss()
            self._impl = "mss"
        except ImportError:
            self._gdi32  = ctypes.windll.gdi32
            self._user32 = ctypes.windll.user32
            self._dc     = self._user32.GetDC(0)
            self._impl   = "gdi"
            print("[sampler] mss not found — falling back to GDI GetPixel")
            print("          GPU content will not be sampled correctly.")
            print("          Fix:  pip install mss")

    def sample(self, x: int, y: int) -> tuple:
        """
        Return (r, g, b) at screen coordinate (x, y).
        Coordinates must be in physical pixels (post-DPI-scale).
        """
        if self._impl == "mss":
            frame = self._sct.grab({"left": x, "top": y, "width": 1, "height": 1})
            # mss pixel format on Windows: BGRA
            b, g, r = frame.raw[0], frame.raw[1], frame.raw[2]
            return int(r), int(g), int(b)
        else:
            c = self._gdi32.GetPixel(self._dc, x, y)
            if c == -1:             # cursor outside the virtual desktop
                return 128, 128, 128
            return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF

    def close(self) -> None:
        if self._impl == "mss":
            self._sct.close()
        else:
            self._user32.ReleaseDC(0, self._dc)


# ═══════════════════════════════════════════════════════════════════
#  LAYER 3 — COLOUR WHEEL  (geometry helpers, pure functions)
# ═══════════════════════════════════════════════════════════════════

def build_wheel_image(size: int, brightness: float = 1.0) -> "Image.Image":
    """
    Generate an HSV colour wheel as a PIL RGBA image.

      angle from centre  →  Hue         (0 – 360°, right = 0° = red)
      distance / radius  →  Saturation  (centre = white, edge = full colour)
      brightness param   →  Value       (constant across the wheel)

    Pixels outside the circle are fully transparent so the canvas
    background shows through the corners.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pix = img.load()
    cx = cy = size * 0.5
    r_max = cx

    for py in range(size):
        for px in range(size):
            dx, dy = px - cx, py - cy
            dist = math.hypot(dx, dy)
            if dist > r_max:
                continue
            hue = math.atan2(-dy, dx) / (2 * math.pi) % 1.0
            sat = dist / r_max
            rv, gv, bv = colorsys.hsv_to_rgb(hue, sat, brightness)
            pix[px, py] = (int(rv * 255), int(gv * 255), int(bv * 255), 255)
    return img


def color_at(canvas_x: int, canvas_y: int,
             cx: float, cy: float, radius: float,
             brightness: float = 1.0):
    """
    Compute the HSV colour at a point in the colour wheel analytically.

    This is the key insight: because we drew the wheel ourselves, we
    know the colour at every pixel without ever reading the screen.
    Returns (r, g, b) or None if (canvas_x, canvas_y) is outside the circle.
    """
    dx, dy = canvas_x - cx, canvas_y - cy
    dist = math.hypot(dx, dy)
    if dist > radius:
        return None
    hue = math.atan2(-dy, dx) / (2 * math.pi) % 1.0
    sat = dist / radius
    rv, gv, bv = colorsys.hsv_to_rgb(hue, sat, brightness)
    return int(rv * 255), int(gv * 255), int(bv * 255)


# ═══════════════════════════════════════════════════════════════════
#  LAYER 3 — GUI APPLICATION
# ═══════════════════════════════════════════════════════════════════

class ColorWheelApp:
    """
    Always-on-top colour wheel window.

    Thread model
    ────────────
    Main thread     tkinter mainloop.  root.after(50 ms) drives the 20 Hz
                    UDP send tick.  All GUI reads happen here.
    pynput thread   mouse.Listener  — writes _mx, _my  (int, GIL-atomic)
    pynput thread   keyboard.Listener — writes _mode   (str, GIL-atomic)

    Because the GIL makes single-object assignments atomic in CPython,
    the int/str writes from pynput threads are safe to read in the main
    thread without an explicit Lock at 20 Hz.

    Colour derivation per mode
    ──────────────────────────
    WHEEL    _on_canvas_motion fires (tkinter main thread) and calls color_at()
             — pure trigonometry, no OS call, no DPI concern.
    AMBIENT  _tick reads (mx, my), scales to physical pixels, calls sampler.sample().
    FROZEN   _tick skips both; _rgb unchanged.
    """

    _MODES = ("WHEEL", "AMBIENT", "FROZEN")

    def __init__(self, govee: GoveeUDP, sampler: ScreenSampler,
                 dpi_scale: float = 1.0):
        self.govee     = govee
        self.sampler   = sampler
        self.dpi_scale = dpi_scale

        # Shared state — written by pynput threads
        self._mx:        int = 0
        self._my:        int = 0
        self._mode:      str = "WHEEL"
        self._prev_mode: str = "WHEEL"   # restored when un-freezing

        # Colour state — main thread only
        self._rgb:      tuple = (255, 255, 255)
        self._prev_rgb: tuple = None

        self._build_ui()
        self._start_listeners()

    # ── UI construction ─────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.root = tk.Tk()
        self.root.title("Govee Colour Wheel")
        self.root.resizable(False, False)
        self.root.wm_attributes("-topmost", True)
        self.root.configure(bg="#0f0f0f")

        win_w = WHEEL_SIZE + WIN_PAD * 2
        win_h = WHEEL_SIZE + WIN_PAD * 2 + 48
        sw    = self.root.winfo_screenwidth()
        sh    = self.root.winfo_screenheight()
        self.root.geometry(f"{win_w}x{win_h}+{(sw - win_w) // 2}+{(sh - win_h) // 2}")

        # ── Wheel canvas ────────────────────────────────────────────
        self.canvas = tk.Canvas(
            self.root,
            width=WHEEL_SIZE, height=WHEEL_SIZE,
            bg="#0f0f0f", highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(padx=WIN_PAD, pady=(WIN_PAD, 4))

        print("[gui] Generating colour wheel … ", end="", flush=True)
        wheel_pil         = build_wheel_image(WHEEL_SIZE, BRIGHTNESS)
        self._wheel_photo = ImageTk.PhotoImage(wheel_pil)
        self.canvas.create_image(0, 0, anchor="nw", image=self._wheel_photo)
        print("done")

        # ── Status bar ──────────────────────────────────────────────
        bar = tk.Frame(self.root, bg="#0f0f0f")
        bar.pack(fill="x", padx=WIN_PAD, pady=(2, 0))

        # Colour swatch
        self._swatch = tk.Label(
            bar, bg="#ffffff", width=3, relief="flat",
        )
        self._swatch.pack(side="left", padx=(0, 7), ipady=6)

        # Mode + RGB readout
        self._status_var = tk.StringVar(value="")
        tk.Label(
            bar,
            textvariable=self._status_var,
            bg="#0f0f0f", fg="#777777",
            font=("Consolas", 9), anchor="w",
        ).pack(side="left")

        # Hint line
        tk.Label(
            self.root,
            text="W wheel · A ambient · Space freeze · click to latch",
            bg="#0f0f0f", fg="#333333",
            font=("Consolas", 8),
        ).pack(pady=(2, WIN_PAD - 4))

        # ── Canvas event bindings ───────────────────────────────────
        self.canvas.bind("<Motion>",   self._on_canvas_motion)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Enter>",    self._on_canvas_enter)
        self.canvas.bind("<Leave>",    self._on_canvas_leave)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Canvas event handlers ────────────────────────────────────────

    def _on_canvas_motion(self, ev) -> None:
        """WHEEL mode: derive colour from cursor geometry — no OS sampling."""
        if self._mode != "WHEEL":
            return
        color = color_at(
            ev.x, ev.y,
            WHEEL_SIZE / 2, WHEEL_SIZE / 2,
            WHEEL_SIZE / 2, BRIGHTNESS,
        )
        if color:
            self._rgb = color

    def _on_canvas_click(self, ev) -> None:
        """Left-click latches the colour under the cursor into FROZEN mode."""
        color = color_at(
            ev.x, ev.y,
            WHEEL_SIZE / 2, WHEEL_SIZE / 2,
            WHEEL_SIZE / 2, BRIGHTNESS,
        )
        if color:
            self._rgb       = color
            self._prev_mode = self._mode
            self._mode      = "FROZEN"

    def _on_canvas_enter(self, _ev) -> None:
        """Mouse entering the wheel window → automatically enable WHEEL mode."""
        if self._mode != "FROZEN":
            self._prev_mode = self._mode
            self._mode = "WHEEL"

    def _on_canvas_leave(self, _ev) -> None:
        """Mouse leaving the wheel window → restore previous mode."""
        if self._mode == "WHEEL":
            self._mode = self._prev_mode

    # ── pynput listeners ─────────────────────────────────────────────

    def _start_listeners(self) -> None:

        def on_move(x, y):
            # Runs in pynput thread at full mouse poll rate (1000 Hz).
            # We just store the latest position; the main thread reads at 20 Hz.
            self._mx = int(x)
            self._my = int(y)

        def on_press(key):
            try:
                ch = key.char
            except AttributeError:
                ch = None

            if ch == "w":
                self._mode = "WHEEL"
            elif ch == "a":
                self._mode = "AMBIENT"
            elif key == pkeyboard.Key.space:
                if self._mode == "FROZEN":
                    self._mode = self._prev_mode
                else:
                    self._prev_mode = self._mode
                    self._mode      = "FROZEN"

        self._ml = pmouse.Listener(on_move=on_move)
        self._kl = pkeyboard.Listener(on_press=on_press)
        self._ml.daemon = self._kl.daemon = True
        self._ml.start()
        self._kl.start()

    # ── 20 Hz send tick (driven by tkinter.after) ────────────────────

    def _tick(self) -> None:
        """
        Called every 50 ms by tkinter.  Samples colour if needed, sends UDP.

        Why root.after() instead of a thread:
          - Runs in the main thread → safe to read/write GUI state.
          - Eliminates the Lock around (mx, my) for AMBIENT sampling.
          - Timer jitter at 50 ms is <5 ms → imperceptible for colour control.
        """
        mode = self._mode   # snapshot; written by kb thread but str assign is atomic

        if mode == "AMBIENT":
            # pynput returns logical coords; scale to physical pixels for mss.
            # On a 1× display dpi_scale == 1.0 and this is a no-op.
            px = int(self._mx * self.dpi_scale)
            py = int(self._my * self.dpi_scale)
            self._rgb = self.sampler.sample(px, py)

        # WHEEL colour is updated synchronously by _on_canvas_motion in this thread.
        # FROZEN: _rgb is unchanged.

        if mode != "FROZEN":
            rgb = self._rgb
            if rgb != self._prev_rgb:
                self.govee.set_color(*rgb)
                self._prev_rgb = rgb

        self._refresh_status(mode)
        self.root.after(1000 // SEND_HZ, self._tick)

    def _refresh_status(self, mode: str) -> None:
        r, g, b = self._rgb
        self._status_var.set(f"[{mode:7s}]  {r:3d}  {g:3d}  {b:3d}")
        self._swatch.configure(bg=f"#{r:02x}{g:02x}{b:02x}")

    # ── Lifecycle ────────────────────────────────────────────────────

    def run(self) -> None:
        self.govee.turn_on()
        self.govee.set_brightness(100)
        self.root.after(1000 // SEND_HZ, self._tick)
        self.root.mainloop()

    def _on_close(self) -> None:
        self._ml.stop()
        self._kl.stop()
        self.sampler.close()
        self.govee.close()
        self.root.destroy()


# ═══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    # PerMonitorV2 DPI awareness — MUST come before tkinter.Tk() and
    # before any GDI/mss call.  Without this, cursor coords from pynput
    # and pixel coords from mss may disagree on scaled (>100%) displays.
    dpi_scale = 1.0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        dpi_scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        pass
    print(f"[init] DPI scale: {dpi_scale:.2f}×")

    # ── Resolve device IP ────────────────────────────────────────────
    if DEVICE_IP:
        device_ip = DEVICE_IP
        print(f"[init] Unicast mode → {device_ip}  (discovery skipped)")
    else:
        print("[init] Scanning LAN for Govee device …")
        try:
            device_ip = discover_device()
            print(f"[init] Found device at {device_ip}")
        except TimeoutError:
            print()
            print("[error] Discovery timed out — router IGMP snooping likely.")
            print()
            print("  Quick fix  — set DEVICE_IP at the top of this script:")
            print('    DEVICE_IP = "192.168.8.xxx"')
            print()
            print("  Permanent fix — SSH into GL.iNet Opal:")
            print("    echo 0 > /sys/devices/virtual/net/br-lan/bridge/multicast_snooping")
            print("    echo 0 > /sys/devices/virtual/net/br-lan/bridge/multicast_querier")
            raise SystemExit(1)

    # ── Launch ───────────────────────────────────────────────────────
    govee   = GoveeUDP(device_ip)
    sampler = ScreenSampler()
    app     = ColorWheelApp(govee, sampler, dpi_scale=dpi_scale)

    print("[run] W = wheel  ·  A = ambient  ·  Space = freeze  ·  click = latch")
    app.run()


if __name__ == "__main__":
    main()
