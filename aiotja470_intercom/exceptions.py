class TJA470Error(Exception):
    """Base exception for TJA470 errors."""

class TJA470ConnectionError(TJA470Error):
    """Exception raised for connection errors."""

class TJA470AuthError(TJA470Error):
    """Exception raised for authentication errors."""

class TJA470ResponseError(TJA470Error):
    """Exception raised for unexpected responses."""
