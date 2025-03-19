# FROM python:3.11-slim
FROM spaneng/doover_device_base:latest AS base_image

## FIRST STAGE ##
#FROM base_image as builder

# Install pipenv requirements
RUN apt-get update && apt-get install -y python3-dev python3-setuptools libjpeg-dev zlib1g-dev gcc
RUN apt-get update && apt-get install -y ffmpeg iputils-ping --no-install-recommends --no-install-suggests && rm -rf /var/lib/apt/lists/*

# Install git for pre-release pydoover
RUN apt update && apt install -y git
RUN pip install pipenv --no-cache-dir
COPY Pipfile .
COPY Pipfile.lock .

ARG PYDOOVER_GITHUB_PAT
RUN PIP_USER=1 PIP_IGNORE_INSTALLED=1 pipenv install --system --deploy --ignore-pipfile --clear

#FROM base_image as final_image

#COPY --from=builder $PYROOT/lib/ $PYROOT/lib/

# ## Install ffmpeg for the different architectures
# # https://docs.docker.com/reference/dockerfile/#automatic-platform-args-in-the-global-scope
# # https://nielscautaerts.xyz/making-dockerfiles-architecture-independent.html
# ARG TARGETPLATFORM
# # if TARGETPLATFORM is set to arm64, install the arm64 version of ffmpeg
# RUN if [ "$TARGETPLATFORM" = "linux/arm64" ]; then apt-get update && apt-get install -y ffmpeg; fi


WORKDIR /app
COPY . /app
ENTRYPOINT ["python3", "application.py"]