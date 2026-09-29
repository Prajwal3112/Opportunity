class WorldBankConnectorError(Exception):
    """Base class for errors whose details are safe for callers to log."""


class WorldBankRequestError(WorldBankConnectorError):
    def __init__(self, message: str, *, url: str, attempts: int) -> None:
        super().__init__(message)
        self.url = url
        self.attempts = attempts


class WorldBankResponseError(WorldBankConnectorError):
    def __init__(self, message: str, *, url: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status_code = status_code


class WorldBankPayloadError(WorldBankConnectorError):
    pass
