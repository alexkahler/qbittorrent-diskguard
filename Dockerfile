FROM python:3.13-slim@sha256:f50f56f1471fc430b394ee75fc826be2d212e35d85ed1171ac79abbba485dce9

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

COPY requirements.lock /app/requirements.lock

RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock

COPY src /app/src

RUN chown -R "${DISKGUARD_UID}:${DISKGUARD_GID}" /app

USER diskguard

EXPOSE 7070

CMD ["python", "-m", "diskguard"]
