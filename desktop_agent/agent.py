#!/usr/bin/env python3
"""Desktop Agent — streams mouse/screen data to the HomeControl API.

This runs on your PC and sends colour commands to the web backend,
keeping PC-specific dependencies (pynput, mss, ctypes/DPI) isolated
from the server.

Modes (keyboard shortcuts, always active):
    A   -> AMBIENT   Sample pixel under cursor, send to API
    M   -> AUDIO     Map desktop audio (loopback) to light colour/brightness
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


# ── Audio sampler (WASAPI loopback via pyaudiowpatch) ────────────

class AudioSampler:
    """Capture Windows loopback audio and derive RGB colour + brightness.

    DSP pipeline (per tick):
        1. Read a chunk of float32 PCM from the WASAPI loopback device.
        2. Mix to mono, compute RMS → brightness (1–100).
        3. Compute FFT, split into three frequency bands:
               Bass   20–250 Hz   → Red
               Mids  250–4 000 Hz → Green
               Treble 4–16 kHz    → Blue
        4. Normalise so the dominant band always hits 255 (vivid colour).
        5. Apply exponential-moving-average smoothing to avoid flicker.
        6. Noise-gate: if RMS < threshold → return black / brightness 1.
    """

    CHUNK = 1024
    BASS = (20, 250)
    MID = (250, 4000)
    TREBLE = (4000, 16000)
    EMA_ALPHA = 0.35          # higher = more responsive, lower = smoother
    NOISE_GATE = 0.0008       # RMS below this → silence

    def __init__(self):
        import pyaudiowpatch as pyaudio
        import numpy as np          # noqa: F401 — kept alive on self

        self._np = np
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self._rate: int = 44100
        self._channels: int = 2

        # smoothed outputs
        self._sr = 0.0
        self._sg = 0.0
        self._sb = 0.0
        self._sbr = 0.0

        self._open_loopback(pyaudio)

    # ── device discovery ──────────────────────────────────────────

    def _open_loopback(self, pyaudio):
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])

        # Find the matching loopback virtual device
        loopback = None
        for dev in self._pa.get_loopback_device_info_generator():
            if speakers["name"] in dev["name"]:
                loopback = dev
                break

        if loopback is None:
            self._pa.terminate()
            raise RuntimeError(
                f"No WASAPI loopback device found for '{speakers['name']}'. "
                "Ensure pyaudiowpatch is installed correctly."
            )

        self._rate = int(loopback["defaultSampleRate"])
        self._channels = loopback["maxInputChannels"]

        self._stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=self._channels,
            rate=self._rate,
            input=True,
            input_device_index=loopback["index"],
            frames_per_buffer=self.CHUNK,
        )

        print(f"[audio] Opened loopback: {loopback['name']}")
        print(f"[audio] Rate: {self._rate} Hz, Channels: {self._channels}")

    # ── per-tick sample ───────────────────────────────────────────

    def sample(self) -> tuple[tuple[int, int, int], int]:
        """Return ``((r, g, b), brightness)`` derived from current audio."""
        np = self._np

        try:
            raw = self._stream.read(self.CHUNK, exception_on_overflow=False)
        except Exception:
            return (0, 0, 0), 1

        samples = np.frombuffer(raw, dtype=np.float32)

        # mix to mono
        if self._channels > 1:
            samples = samples.reshape(-1, self._channels).mean(axis=1)

        # RMS → brightness
        rms = float(np.sqrt(np.mean(samples ** 2)))

        if rms < self.NOISE_GATE:
            # silence — fade smoothly to off
            a = self.EMA_ALPHA
            self._sr *= (1 - a)
            self._sg *= (1 - a)
            self._sb *= (1 - a)
            self._sbr *= (1 - a)
            return (
                (int(self._sr), int(self._sg), int(self._sb)),
                max(1, int(self._sbr)),
            )

        bright_raw = min(rms * 330, 100.0)

        # FFT
        fft_mag = np.abs(np.fft.rfft(samples))
        freqs = np.fft.rfftfreq(len(samples), 1.0 / self._rate)

        bass = self._band_energy(fft_mag, freqs, *self.BASS)
        mids = self._band_energy(fft_mag, freqs, *self.MID)
        treble = self._band_energy(fft_mag, freqs, *self.TREBLE)

        peak = max(bass, mids, treble, 1e-10)
        r_raw = bass / peak * 255.0
        g_raw = mids / peak * 255.0
        b_raw = treble / peak * 255.0

        # EMA smoothing
        a = self.EMA_ALPHA
        self._sr = a * r_raw + (1 - a) * self._sr
        self._sg = a * g_raw + (1 - a) * self._sg
        self._sb = a * b_raw + (1 - a) * self._sb
        self._sbr = a * bright_raw + (1 - a) * self._sbr

        r = max(0, min(255, int(self._sr)))
        g = max(0, min(255, int(self._sg)))
        b = max(0, min(255, int(self._sb)))
        brightness = max(1, min(100, int(self._sbr)))

        return (r, g, b), brightness

    @staticmethod
    def _band_energy(fft_mag, freqs, lo, hi):
        import numpy as np
        mask = (freqs >= lo) & (freqs <= hi)
        if not mask.any():
            return 0.0
        return float(np.sqrt(np.mean(fft_mag[mask] ** 2)))

    def close(self):
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        self._pa.terminate()


# ── Agent ─────────────────────────────────────────────────────────

class DesktopAgent:
    """Streams mouse position / ambient colour to the HomeControl API."""

    def __init__(self, api_base: str, send_hz: int = 20):
        self._api = api_base.rstrip("/")
        self._interval = 1.0 / send_hz
        self._sampler = ScreenSampler()
        self._audio: AudioSampler | None = None   # lazy — created on first M press
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
        print("[agent] A = ambient | M = audio | F = freeze | Q = quit")

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

        if self._mode == "AUDIO":
            self._tick_audio()
        else:
            self._tick_ambient()

    def _tick_ambient(self):
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
            pass

    def _tick_audio(self):
        if self._audio is None:
            return

        rgb, _brightness = self._audio.sample()

        if rgb != self._prev_rgb:
            self._prev_rgb = rgb
            r, g, b = rgb
            try:
                requests.post(
                    f"{self._api}/govee/color",
                    json={"r": r, "g": g, "b": b},
                    timeout=0.1,
                )
            except requests.RequestException:
                pass

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
                self._prev_rgb = None
                print("[mode] AMBIENT")
            elif ch == "m":
                if self._audio is None:
                    try:
                        self._audio = AudioSampler()
                    except Exception as exc:
                        print(f"[audio] Failed to start: {exc}")
                        print("[audio] pip install PyAudioWPatch numpy")
                        return
                self._mode = "AUDIO"
                self._prev_rgb = None
                try:
                    requests.post(
                        f"{self._api}/govee/brightness",
                        json={"brightness": 100},
                        timeout=0.5,
                    )
                except requests.RequestException:
                    pass
                print("[mode] AUDIO")
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
        if self._audio:
            self._audio.close()


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
