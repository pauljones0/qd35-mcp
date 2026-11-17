class QD35Error(Exception):
    """Base class for expected QD35 errors."""

    def __init__(self, message: str, *, connection_dropped: bool | None = None) -> None:
        super().__init__(message)
        self.connection_dropped = connection_dropped


class DeviceNotFound(QD35Error):
    pass


class ProtocolError(QD35Error):
    pass


class RequestTimeout(QD35Error):
    pass


class TransportError(QD35Error):
    """Bluetooth adapter, permission, scan, or connection failure."""


class OutcomeUnknown(QD35Error):
    """A write was attempted but its physical outcome needs independent readback."""


class WritesDisabled(QD35Error):
    pass


class DangerousWritesDisabled(QD35Error):
    pass
