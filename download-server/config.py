import os

# Secret used to sign download tokens + passcode hashes.
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = os.environ.get("DOWNLOAD_SECRET_KEY", "change-this-before-hosting")

# Public URL of this server (used in the copy-able download command).
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8080")

# How long a client download token stays valid after email verification.
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "24"))

# Admin credentials — MUST be overridden via env vars before going public.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-this-password")

# Admin session cookie lifetime.
ADMIN_SESSION_TTL_HOURS = int(os.environ.get("ADMIN_SESSION_TTL_HOURS", "8"))

# How long the one-time passcode-reveal token is valid (admin use only).
PASSCODE_REVEAL_TTL_MINUTES = int(os.environ.get("PASSCODE_REVEAL_TTL_MINUTES", "15"))
