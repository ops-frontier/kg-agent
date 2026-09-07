FROM node:22-bookworm AS frontend

WORKDIR /build
COPY package.json package-lock.json vite.config.js index.html ./
COPY public/ ./public/
COPY src/ ./src/
RUN npm ci && npm run build

FROM python:3.12-slim-bookworm

WORKDIR /opt/kg-agent
COPY server.py ./server.py
COPY --from=frontend /build/dist ./dist

ENV PORT=8080
ENV STATIC_DIR=/opt/kg-agent/dist
EXPOSE 8080

ENTRYPOINT ["python", "server.py"]