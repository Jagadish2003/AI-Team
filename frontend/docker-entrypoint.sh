#!/bin/sh
set -e

SSL_CERT=/etc/nginx/ssl/cert.pem
SSL_KEY=/etc/nginx/ssl/key.pem
TEMPLATES=/etc/nginx/templates
CONF=/etc/nginx/conf.d/default.conf

if [ -f "$SSL_CERT" ] && [ -f "$SSL_KEY" ]; then
    echo "[agentiq-frontend] SSL certs found — enabling HTTPS"
    cp "$TEMPLATES/nginx-ssl.conf.template" "$CONF"
else
    echo "[agentiq-frontend] No SSL certs — serving HTTP on port 80"
    cp "$TEMPLATES/nginx-http.conf.template" "$CONF"
fi

exec nginx -g 'daemon off;'
