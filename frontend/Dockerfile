# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ Frontend
# Stage 1: Build Vite/React SPA
# Stage 2: Serve via nginx with HTTP/HTTPS auto-selection
# ─────────────────────────────────────────────────────────────────────────────

# Build:  docker build -t agentiq-frontend:1.0 .
# Run:    docker run -p 80:80 agentiq-frontend:1.0
# ─────────────────────────────────────────────────────────────────────────────

FROM node:20-alpine AS build

WORKDIR /app

COPY package*.json ./
RUN npm ci

# VITE_API_BASE_URL="" → all /api/ calls are relative → nginx proxies to backend
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

COPY . .
RUN npm run build


FROM nginx:1.27-alpine

# Static assets
COPY --from=build /app/dist /usr/share/nginx/html

# Nginx config templates — entrypoint picks HTTP or HTTPS at startup
RUN mkdir -p /etc/nginx/templates
COPY nginx-http.conf.template /etc/nginx/templates/
COPY nginx-ssl.conf.template  /etc/nginx/templates/

# SSL cert mount point (optional — volume-mount from host when available)
RUN mkdir -p /etc/nginx/ssl

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 80 443

# Health check hits the /health endpoint defined in nginx config
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD wget -qO- http://localhost/health || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
