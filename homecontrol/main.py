"""HomeControl — FastAPI entry point.

Start the server:
    uvicorn homecontrol.main:app --host 0.0.0.0 --port 8000 --reload

Or directly:
    python -m homecontrol.main
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .controllers.govee import GoveeController
from .controllers.kauf import KaufPlugController
from .controllers.samsung_tv import SamsungTVController
from .routers import govee as govee_router
from .routers import kauf as kauf_router
from .routers import samsung_tv as tv_router
from .routers import visualizer as viz_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config from environment (or defaults) ─────────────────────────
GOVEE_IP = os.getenv("GOVEE_IP", "192.168.8.233")
SAMSUNG_TV_IP = os.getenv("SAMSUNG_TV_IP", "")   # leave blank to skip
SAMSUNG_TV_MAC = os.getenv("SAMSUNG_TV_MAC", "")  # e.g. "64:E7:D8:9F:42:06" for WoL
KAUF_PLUG_IP = os.getenv("KAUF_PLUG_IP", "192.168.8.166")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Connect device controllers on startup, disconnect on shutdown."""

    # Govee
    govee = GoveeController(ip=GOVEE_IP or None, name="Govee Light")
    try:
        govee.connect()
        govee.power_on()
        govee.set_brightness(100)
    except Exception as exc:
        logger.error("Govee connection failed: %s", exc)
    govee_router.set_controller(govee)

    # Samsung TV (optional)
    tv = None
    if SAMSUNG_TV_IP:
        tv = SamsungTVController(ip=SAMSUNG_TV_IP, mac=SAMSUNG_TV_MAC or None)
        try:
            tv.connect()
        except Exception as exc:
            logger.error("Samsung TV connection failed: %s", exc)
        tv_router.set_controller(tv)
    else:
        logger.info("SAMSUNG_TV_IP not set — TV control disabled")

    # KAUF Smart Plug
    kauf = KaufPlugController(ip=KAUF_PLUG_IP, name="Aquarium Tank Light")
    try:
        kauf.connect()
    except Exception as exc:
        logger.error("KAUF plug connection failed: %s", exc)
    kauf_router.set_controller(kauf)

    yield

    # Shutdown
    govee.disconnect()
    if tv:
        tv.disconnect()


app = FastAPI(
    title="HomeControl",
    description="Local home automation API — Govee lights, Samsung TV, and more.",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS — required because the web UI is served from the router ──
# (e.g. http://192.168.8.1) and TV calls arrive cross-origin at this PC.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # LAN only — safe for home network
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(govee_router.router)
app.include_router(tv_router.router)
app.include_router(kauf_router.router)
app.include_router(viz_router.router)

# ── Static frontend ───────────────────────────────────────────────
_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def root():
    """Serve the web UI."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/devices")
def list_devices():
    """Return status of all registered controllers."""
    devices = []
    if govee_router._ctrl:
        devices.append(govee_router._ctrl.status())
    if tv_router._ctrl:
        devices.append(tv_router._ctrl.status())
    if kauf_router._ctrl:
        devices.append(kauf_router._ctrl.status())
    return devices


# Mount static files AFTER API routes so /govee/* and /tv/* are not shadowed
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


# Allow running directly: python -m homecontrol.main
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("homecontrol.main:app", host="0.0.0.0", port=8000, reload=True)
