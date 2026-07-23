FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG DEBIAN_MIRROR=
ARG PIP_INDEX_URL=https://pypi.org/simple

RUN if [ -n "$DEBIAN_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org|$DEBIAN_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi

RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        ffmpeg \
        fonts-wqy-microhei \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV XDG_CACHE_HOME=/tmp/vthumb/cache

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt

COPY app ./app

RUN useradd -r -u 10001 vthumb \
    && mkdir -p /tmp/vthumb/cache \
    && chown -R vthumb:vthumb /app /tmp/vthumb

USER vthumb

CMD ["python", "-m", "app.bot"]
