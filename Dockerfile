# Galatea Link 容器镜像，默认提供轻量 ONNX Runtime 服务

FROM python:3.11-slim

ARG LINK_VERSION=3.1.0
ARG LINK_REQUIREMENTS=requirements-onnx.txt

LABEL org.opencontainers.image.title="Galatea Link" \
      org.opencontainers.image.version="${LINK_VERSION}" \
      org.opencontainers.image.description="YGOPro、Galatea Core 与外部智能体之间的异步连接服务"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GALATEA_LINK_DATA_DIR=/data

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-base.txt requirements-onnx.txt requirements-pytorch.txt requirements-semantic.txt requirements.txt ./
RUN case "$LINK_REQUIREMENTS" in \
      requirements-onnx.txt|requirements-pytorch.txt|requirements.txt) ;; \
      *) echo "Unsupported requirements profile: $LINK_REQUIREMENTS" >&2; exit 2 ;; \
    esac \
    && python -m pip install --no-cache-dir -r "$LINK_REQUIREMENTS"

COPY . .

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin galatea \
    && mkdir -p /data \
    && chown -R galatea:galatea /data

USER galatea

EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/v1/health', timeout=3).read()"]

ENTRYPOINT ["python", "scripts/docker_entrypoint.py"]
