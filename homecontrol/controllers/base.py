"""Abstract base class for all home automation device controllers."""

from abc import ABC, abstractmethod
from typing import Any


class DeviceController(ABC):
    """Base interface that every device controller must implement.

    Subclasses handle protocol specifics (UDP, WebSocket, HTTP, etc.)
    while this class defines the common surface the API layer talks to.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection / discover the device on the network."""

    @abstractmethod
    def disconnect(self) -> None:
        """Release sockets / sessions."""

    @abstractmethod
    def power_on(self) -> None: ...

    @abstractmethod
    def power_off(self) -> None: ...

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """Return a dict with at least {"online": bool}."""

    @property
    @abstractmethod
    def device_type(self) -> str:
        """Short identifier like 'govee_light' or 'samsung_tv'."""
