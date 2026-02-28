"""Pydantic models for the HomeControl API request/response schemas."""

from pydantic import BaseModel, Field


# ── Request bodies ────────────────────────────────────────────────

class ColorRequest(BaseModel):
    r: int = Field(..., ge=0, le=255)
    g: int = Field(..., ge=0, le=255)
    b: int = Field(..., ge=0, le=255)


class BrightnessRequest(BaseModel):
    brightness: int = Field(..., ge=1, le=100)


class ColorTempRequest(BaseModel):
    kelvin: int = Field(..., ge=2000, le=9000)


class PowerRequest(BaseModel):
    on: bool


class TVKeyRequest(BaseModel):
    key: str = Field(..., examples=["KEY_VOLUP", "KEY_MUTE", "KEY_POWER"])


class TVSourceRequest(BaseModel):
    source: str = Field(..., examples=["HDMI1", "HDMI2", "TV"])


class TVVolumeRequest(BaseModel):
    level: int = Field(..., ge=0, le=100)


class TVAppRequest(BaseModel):
    app_id: str


class TVVolumeDeltaRequest(BaseModel):
    delta: int = Field(..., description="Positive = KEY_VOLUP steps, negative = KEY_VOLDOWN steps")


# ── Response bodies ───────────────────────────────────────────────

class DeviceStatus(BaseModel):
    online: bool
    device_type: str
    name: str
    ip: str | None = None
    power: bool = False
    color: tuple[int, int, int] | None = None
    brightness: int | None = None


class MessageResponse(BaseModel):
    message: str
