class ConnectorError(Exception):
    """Base class. Keeps the original driver exception for debugging."""
    def __init__(self, message: str, original: Exception | None=None):
        super().__init__(message)
        self.original=original

class ConnectorAuthError(ConnectorError): ...       # wrong/expired password, disabled login
class ConnectorNetworkError(ConnectionError): ...   # host unreachable, port closed, dropped connection
class ConnectorTimeoutError(ConnectorError): ...    # login or query timeout
class ConnectorPermissionError(ConnectorError): ...  # authenticated, but not allowed
class ConnectorConfigError(ConnectorError): ...      # missing ODBC driver, untrusted TLS cert