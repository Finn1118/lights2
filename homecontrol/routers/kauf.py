"""FastAPI router for KAUF smart plug control."""

from fastapi import APIRouter, HTTPException

from ..controllers.kauf import KaufPlugController
from ..models import DeviceStatus, MessageResponse, PowerRequest

router = APIRouter(prefix="/kauf", tags=["kauf"])

# Populated by main.py at startup via set_controller()
_ctrl: KaufPlugController | None = None


def set_controller(ctrl: KaufPlugController) -> None:
    global _ctrl
    _ctrl = ctrl


def _get_ctrl() -> KaufPlugController:
    if _ctrl is None or not _ctrl.status()["online"]:
        raise HTTPException(503, "KAUF plug is not connected")
    return _ctrl


# ── endpoints ─────────────────────────────────────────────────────

@router.get("/status", response_model=DeviceStatus)
def get_status():
    if _ctrl is None:
        raise HTTPException(503, "KAUF plug is not configured")
    return _ctrl.status()


@router.post("/power", response_model=MessageResponse)
def set_power(body: PowerRequest):
    ctrl = _get_ctrl()
    if body.on:
        ctrl.power_on()
    else:
        ctrl.power_off()
    return {"message": f"Plug {'on' if body.on else 'off'}"}
