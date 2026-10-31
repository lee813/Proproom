# Proproom — one container, one folder.
# Tiers (handover §4.2): this file builds :latest-cuda by default. Intel/CPU tiers swap BASE_IMAGE and EXTRAS.
#   docker build -t proproom:cuda .
#   docker build -t proproom:cpu --build-arg BASE_IMAGE=python:3.12-slim-bookworm .
ARG BASE_IMAGE=pytorch/pytorch:2.14.0-cuda13.0-cudnn9-runtime
FROM ${BASE_IMAGE}

ARG EXTRAS=dev
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_ROOT_USER_ACTION=ignore \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    PROP_NAS=/nas \
    HOME=/home/prop

# ffmpeg: scene cuts, loudness, clip delivery. Built with nvdec/cuvid on Ubuntu.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /home/prop && chmod 1777 /home/prop

WORKDIR /opt/proproom
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY proproom ./proproom
RUN pip install ".${EXTRAS:+[$EXTRAS]}"
COPY tests ./tests

VOLUME ["/nas"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4).status == 200 else 1)"

CMD ["prop", "serve"]
