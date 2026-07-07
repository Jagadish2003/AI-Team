FROM node:20-alpine AS builder

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .

ENV VITE_API_BASE_URL=""
ENV VITE_DEV_JWT="dev-token-change-me"

RUN npm run build

FROM nginx:1.27-alpine

COPY --from=builder /app/dist /usr/share/nginx/html

# HTTP-only config (default). SSL config is present but only activated by
# 99-select-ssl.sh when certs are mounted at /etc/nginx/ssl/ at runtime.
COPY nginx.conf     /etc/nginx/conf.d/default.conf
COPY nginx-ssl.conf /etc/nginx/conf.d/nginx-ssl.conf

# Selector script: runs before nginx starts and swaps in the SSL config if certs exist.
COPY 99-select-ssl.sh /docker-entrypoint.d/99-select-ssl.sh
RUN chmod +x /docker-entrypoint.d/99-select-ssl.sh

EXPOSE 80 443

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost/health || exit 1
