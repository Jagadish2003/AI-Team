import os

# Secret used to sign download tokens — MUST be set via env var in production.
# Generate: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = os.environ.get("DOWNLOAD_SECRET_KEY", "change-this-before-hosting")

# Public URL of this server (shown in the copy-able download command).
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8080")

# How long a download token is valid after the email is verified.
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "24"))
