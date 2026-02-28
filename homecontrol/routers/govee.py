"""FastAPI router for Govee light control."""

from fastapi import APIRouter, HTTPException

from ..controllers.govee import GoveeController
from ..models import (
    BrightnessRequest,
    ColorRequest,
    ColorTempRequest,
    DeviceStatus,
    MessageResponse,
    PowerRequest,
)

router = APIRouter(prefix="/govee", tags=["govee"])

# Populated by main.py at startup via set_controller()
_ctrl: GoveeController | None = None


def set_controller(ctrl: GoveeController) -> None:
    global _ctrl
    _ctrl = ctrl


def _get_ctrl() -> GoveeController:
    if _ctrl is None or not _ctrl.status()["online"]:
        raise HTTPException(503, "Govee controller is not connected")
    return _ctrl


# ── endpoints ─────────────────────────────────────────────────────

@router.get("/status", response_model=DeviceStatus)
def get_status():
    return _get_ctrl().status()


@router.post("/power", response_model=MessageResponse)
def set_power(body: PowerRequest):
    ctrl = _get_ctrl()
    if body.on:
        ctrl.power_on()
    else:
        ctrl.power_off()
    return {"message": f"Power {'on' if body.on else 'off'}"}


@router.post("/color", response_model=MessageResponse)
def set_color(body: ColorRequest):
    ctrl = _get_ctrl()
    ctrl.set_color(body.r, body.g, body.b)
    return {"message": f"Color set to ({body.r}, {body.g}, {body.b})"}


@router.post("/brightness", response_model=MessageResponse)
def set_brightness(body: BrightnessRequest):
    ctrl = _get_ctrl()
    ctrl.set_brightness(body.brightness)
    return {"message": f"Brightness set to {body.brightness}%"}


@router.post("/color_temp", response_model=MessageResponse)
def set_color_temp(body: ColorTempRequest):
    ctrl = _get_ctrl()
    ctrl.set_color_temp(body.kelvin)
    return {"message": f"Color temperature set to {body.kelvin}K"}
