FROM python:3.13-slim

COPY ./requirements.txt /requirements.txt

ARG TARGETARCH=amd64
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends build-essential pkg-config curl \
    && case "${TARGETARCH}" in amd64|arm64) ARCH="${TARGETARCH}" ;; *) ARCH=amd64 ;; esac \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-${ARCH}" \
         -o /usr/local/bin/supercronic \
    && chmod +x /usr/local/bin/supercronic \
    && rm -rf /var/lib/apt/lists/*

# Store HF model cache inside /py so it's readable by all users
ENV HF_HOME=/py/huggingface
ENV UV_INDEX_STRATEGY=unsafe-best-match

COPY ./init_models.py /init_models.py

RUN pip install --no-cache-dir uv \
    && uv venv /py \
    && uv pip install --python /py/bin/python -r /requirements.txt \
    && /py/bin/python /init_models.py \
    && adduser --disabled-password djangouser \
    && chown -R djangouser /py/huggingface

COPY . /app
WORKDIR /app

ENV PATH="/py/bin:$PATH"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CUDA_VISIBLE_DEVICES=""
ENV PYTHONPATH="/app"

RUN mkdir -p /app/.static && chown -R djangouser . \
    && chmod +x ./release.sh ./manage.py

EXPOSE 8000

USER djangouser
ENTRYPOINT ["./release.sh"]
