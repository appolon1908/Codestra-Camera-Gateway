# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY requirements.txt pyproject.toml ./
COPY camera_gateway ./camera_gateway
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r requirements.txt \
 && /opt/venv/bin/pip install --no-deps .

FROM python:3.12-slim-bookworm AS runtime
# ffmpeg/ffprobe provide RTSP snapshot, stream test and MJPEG preview.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 gateway \
 && useradd --system --uid 10001 --gid gateway --home-dir /nonexistent --shell /usr/sbin/nologin gateway \
 && install -d -o gateway -g gateway -m 0750 /var/lib/camera-gateway
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CAMERA_GATEWAY_ENV=production \
    CAMERA_GATEWAY_DATABASE_PATH=/var/lib/camera-gateway/cameras.db
USER 10001:10001
EXPOSE 8080
VOLUME ["/var/lib/camera-gateway"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"]
ENTRYPOINT ["camera-gateway"]
