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

# Resolve base-provided packages from the system instead of reinstalling them.
RUN uv venv --system-site-packages

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    SKIP=$(uv pip freeze --system | sed -E 's/[[:space:]@=].*//; s/^/--no-install-package /' | tr '\n' ' ') && \
    uv sync --locked --no-install-project --no-dev $SKIP

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    SKIP=$(uv pip freeze --system | sed -E 's/[[:space:]@=].*//; s/^/--no-install-package /' | tr '\n' ' ') && \
    uv sync --locked --no-dev $SKIP


# --- ffmpeg-fetch: download a static ffmpeg binary (full variant only) --------
# The binary is fully static, so it runs unchanged on the musl-based alpine
# image (and at ~49MB is smaller than alpine's native `apk add ffmpeg`, ~103MB).
# These are johnvansickle.com builds, re-hosted in our own public S3 bucket:
# upstream has had extended outages that broke this build outright, and the
# version is pinned in the key so a build is always reproducible.
FROM base AS ffmpeg-fetch
ARG TARGETARCH
ARG FFMPEG_VERSION=7.0.2
ARG FFMPEG_BASE_URL=https://doover-public.s3.ap-southeast-2.amazonaws.com/ffmpeg
RUN set -eux; \
    case "$TARGETARCH" in \
        arm64) FF_ARCH=arm64; FF_SHA256=6bb182d0d75d23028db82e9e4f723ca69b853d055698486e6984ddb2c06fb8ce ;; \
        amd64) FF_ARCH=amd64; FF_SHA256=e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99 ;; \
        *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    mkdir -p /opt/ffmpeg; \
    curl -fsSL "${FFMPEG_BASE_URL}/ffmpeg-${FFMPEG_VERSION}-${FF_ARCH}-static" -o /opt/ffmpeg/ffmpeg; \
    echo "${FF_SHA256}  /opt/ffmpeg/ffmpeg" | sha256sum -c -; \
    chmod +x /opt/ffmpeg/ffmpeg


# --- runtime: common runtime layer shared by both variants -------------------
FROM base AS runtime
# iputils covers the base ping() path (generic RTSP cameras); API cameras
# ping over HTTP and don't need it.
RUN apk add --no-cache iputils
COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH"
CMD ["doover-app-run"]


# --- full: runtime + static ffmpeg -------------------------------------------
FROM runtime AS full
COPY --from=ffmpeg-fetch /opt/ffmpeg/ffmpeg /usr/local/bin/ffmpeg


# --- slim: the default variant (no ffmpeg). Kept last so a plain
#     `docker build` with no --target produces the slim image. -----------------
FROM runtime AS slim
