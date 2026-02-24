FROM python:3.12-slim

LABEL org.opencontainers.image.title="DiskGuard" \
      org.opencontainers.image.description="DiskGuard - qBittorrent disk safety valve" \
      org.opencontainers.image.source="https://github.com/alexkahler/qbittorrent-diskguard" \
      org.opencontainers.image.licenses="GNU GPL-3.0-only"

ARG DISKGUARD_UID=1000
ARG DISKGUARD_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN groupadd --gid "${DISKGUARD_GID}" diskguard \
    && useradd --uid "${DISKGUARD_UID}" --gid "${DISKGUARD_GID}" --home-dir /app --create-home --shell /usr/sbin/nologin diskguard

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src

RUN chown -R "${DISKGUARD_UID}:${DISKGUARD_GID}" /app

USER diskguard

EXPOSE 7070

CMD ["python", "-m", "diskguard"]
