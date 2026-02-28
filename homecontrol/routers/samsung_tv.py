"""FastAPI router for Samsung TV control."""

import time

from fastapi import APIRouter, HTTPException

from ..controllers.samsung_tv import SamsungTVController
from ..models import (
    DeviceStatus,
    MessageResponse,
    PowerRequest,
    TVAppRequest,
    TVKeyRequest,
    TVSourceRequest,
    TVVolumeDeltaRequest,
    TVVolumeRequest,
)

router = APIRouter(prefix="/tv", tags=["samsung_tv"])

_ctrl: SamsungTVController | None = None


def set_controller(ctrl: SamsungTVController) -> None:
    global _ctrl
    _ctrl = ctrl


def _get_ctrl() -> SamsungTVController:
    if _ctrl is None:
        raise HTTPException(503, "Samsung TV controller is not configured")
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


@router.post("/key", response_model=MessageResponse)
def send_key(body: TVKeyRequest):
    ctrl = _get_ctrl()
    ctrl.send_key(body.key)
    return {"message": f"Sent key: {body.key}"}


@router.post("/source", response_model=MessageResponse)
def set_source(body: TVSourceRequest):
    ctrl = _get_ctrl()
    ctrl.set_source(body.source)
    return {"message": f"Source set to {body.source}"}


@router.post("/volume", response_model=MessageResponse)
def set_volume(body: TVVolumeRequest):
    ctrl = _get_ctrl()
    ctrl.set_volume(body.level)
    return {"message": f"Volume set to {body.level}"}


@router.post("/volume-delta", response_model=MessageResponse)
def send_volume_delta(body: TVVolumeDeltaRequest):
    if body.delta == 0:
        return {"message": "No change"}
    ctrl = _get_ctrl()
    key = "KEY_VOLUP" if body.delta > 0 else "KEY_VOLDOWN"
    count = abs(body.delta)
    for i in range(count):
        ctrl.send_key(key)
        if i < count - 1:
            time.sleep(0.01)
    direction = "up" if body.delta > 0 else "down"
    return {"message": f"Volume {direction} ×{count}"}


@router.post("/repeat/start", response_model=MessageResponse)
def repeat_start(body: TVKeyRequest):
    _get_ctrl().start_repeat(body.key)
    return {"message": f"Repeat started: {body.key}"}


@router.post("/repeat/stop", response_model=MessageResponse)
def repeat_stop():
    _get_ctrl().stop_repeat()
    return {"message": "Repeat stopped"}


@router.post("/app", response_model=MessageResponse)
def launch_app(body: TVAppRequest):
    ctrl = _get_ctrl()
    ctrl.launch_app(body.app_id)
    return {"message": f"Launched app: {body.app_id}"}
