import os

def is_live() -> bool:
    """
    Returns True if INGEST_MODE environment variable is 'live',
    otherwise returns False (defaults to offline).
    """
    return os.environ.get("INGEST_MODE", "offline").lower() == "live"
