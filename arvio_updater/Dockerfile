ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache python3

WORKDIR /app
COPY updater.py /app/updater.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL \
  io.hass.name="Arvio Updater" \
  io.hass.version="0.1.0" \
  io.hass.type="addon" \
  io.hass.arch="aarch64|amd64|armv7"

CMD [ "/run.sh" ]
