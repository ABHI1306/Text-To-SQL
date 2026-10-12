class PortError(RuntimeError):
    """Base error raised when a port operation fails."""


class PortUnavailable(PortError):
    """The external service could not be reached."""


class PortTimeout(PortError):
    """The external service did not respond in time."""