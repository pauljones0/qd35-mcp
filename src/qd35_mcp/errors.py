class QD35Error(Exception):
    """Base class for expected QD35 errors."""


class DeviceNotFound(QD35Error):
    pass


class ProtocolError(QD35Error):
    pass


class RequestTimeout(QD35Error):
    pass


class TransportError(QD35Error):
    """Bluetooth adapter, permission, scan, or connection failure."""


class OutcomeUnknown(QD35Error):
    """The connection dropped after a write, so physical outcome needs readback."""


class WritesDisabled(QD35Error):
    pass


class DangerousWritesDisabled(QD35Error):
    pass
