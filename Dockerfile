ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE} AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app
COPY backend/requirements.lock backend/requirements.lock
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r backend/requirements.lock \
    && useradd --uid 10001 --create-home app
COPY backend /app/backend
COPY --from=frontend-builder /build/frontend/dist /app/frontend/dist
USER 10001
EXPOSE 8000
CMD ["uvicorn","app:app","--app-dir","/app/backend","--host","0.0.0.0","--port","8000","--workers","1","--no-access-log"]
