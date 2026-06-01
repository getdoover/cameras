# syntax=docker/dockerfile:1
#
# Multi-stage build producing two image variants:
#   * slim (default) -- no ffmpeg. Works for Dahua/Hikvision/Bosch cameras taking
#                       image (still) snapshots via the camera HTTP API.
#   * full           -- slim + a static ffmpeg binary, required for generic RTSP
#                       cameras and for video-mode snapshots on any camera.
#
# Build the default (slim):   docker build -t cameras:slim .
# Build the full variant:     docker build --target full -t cameras:full .
#
# Dependency resolution (uv) happens only in the `builder` stage and the
# resulting venv is copied into the runtime, so build tooling never ships. All
# deps install from wheels, so no compilers/dev headers are needed.

FROM spaneng/doover_device_base AS base
LABEL com.doover.app="true"
LABEL com.doover.managed="true"
HEALTHCHECK --interval=30s --timeout=2s --start-period=5s CMD curl -f "127.0.0.1:$HEALTHCHECK_PORT" || exit 1
WORKDIR /app


# --- builder: resolve and install python deps into /app/.venv ----------------
FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:0.7.3 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV UV_PYTHON_DOWNLOADS=0

# give the app access to our pre-installed system packages
RUN uv venv --system-site-packages

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


# --- ffmpeg-fetch: download a static ffmpeg binary (full variant only) --------
FROM base AS ffmpeg-fetch
ARG TARGETARCH
RUN apt update && apt install -y --no-install-recommends curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "$TARGETARCH" in \
        arm64) FF_ARCH=arm64 ;; \
        amd64) FF_ARCH=amd64 ;; \
        arm)   FF_ARCH=armhf ;; \
        *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FF_ARCH}-static.tar.xz" -o /tmp/ffmpeg.tar.xz; \
    mkdir -p /opt/ffmpeg; \
    tar -xJf /tmp/ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1


# --- runtime: common runtime layer shared by both variants -------------------
FROM base AS runtime
# iputils-ping covers the base ping() path (generic RTSP cameras); API cameras
# ping over HTTP and don't need it.
RUN apt update && apt install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH"
CMD ["doover-app-run"]


# --- full: runtime + static ffmpeg -------------------------------------------
FROM runtime AS full
COPY --from=ffmpeg-fetch /opt/ffmpeg/ffmpeg /usr/local/bin/ffmpeg


# --- slim: the default variant (no ffmpeg). Kept last so a plain
#     `docker build` with no --target produces the slim image. -----------------
FROM runtime AS slim
