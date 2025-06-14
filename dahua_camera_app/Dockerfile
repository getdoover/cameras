FROM spaneng/doover_device_base AS base_image

# Install ffmpeg, etc. requirements
RUN apt update && apt install -y python3-dev python3-setuptools libjpeg-dev zlib1g-dev gcc
RUN apt update && apt install -y ffmpeg iputils-ping --no-install-recommends --no-install-suggests && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.7.3 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN apt update && apt install -y git

# give the app access to our pipenv installed packages
RUN uv venv --system-site-packages

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"
CMD ["doover-app-run"]
