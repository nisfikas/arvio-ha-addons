ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip \
  && pip3 install --break-system-packages --no-cache-dir websocket-client==1.8.0
# 0.1.21 «Μουσική»: Pillow resizes album art to ≤ 256 px JPEG (arvio.media_art). Wheels exist for
# amd64/aarch64 (musllinux); armv7 falls back to Alpine's py3-pillow. Without Pillow the agent
# still runs — it only returns small originals (≤ 40 KB) and answers `too_large` otherwise.
RUN pip3 install --break-system-packages --no-cache-dir pillow==11.1.0 \
  || apk add --no-cache py3-pillow

WORKDIR /app
COPY agent.py /app/agent.py
COPY ui.html /app/ui.html
COPY partner.html /app/partner.html
COPY home.html /app/home.html
COPY remote.html /app/remote.html
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL \
  io.hass.name="Arvio Agent" \
  io.hass.version="0.1.21" \
  io.hass.type="addon" \
  io.hass.arch="aarch64|amd64|armv7"

CMD [ "/run.sh" ]
