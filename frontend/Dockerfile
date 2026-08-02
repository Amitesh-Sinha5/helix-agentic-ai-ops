# syntax=docker/dockerfile:1

# --- builder ---------------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /app

# Lockfile-only install first, so the dependency layer caches independently of
# source changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines env vars at build time, so the API URL is a build arg rather than
# a runtime one. A different backend means a different image.
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=$VITE_API_URL

RUN npm run build

# --- runtime ---------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

RUN apk add --no-cache curl

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
