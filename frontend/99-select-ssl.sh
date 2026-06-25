#!/bin/sh
# Runs inside the nginx container via /docker-entrypoint.d/ before nginx starts.
# Switches to the HTTPS config when certs are mounted at /etc/nginx/ssl/.
set -e

CERT="/etc/nginx/ssl/fullchain.pem"
KEY="/etc/nginx/ssl/privkey.pem"
SSL_CONF="/etc/nginx/conf.d/nginx-ssl.conf"
DEFAULT_CONF="/etc/nginx/conf.d/default.conf"

if [ -f "$CERT" ] && [ -f "$KEY" ]; then
    echo "[nginx-ssl] Certificates found — switching to HTTPS config"
    cp "$SSL_CONF" "$DEFAULT_CONF"
else
    echo "[nginx-ssl] No certificates at /etc/nginx/ssl — running HTTP only"
fi
