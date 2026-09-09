ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache python3

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
  io.hass.version="0.1.9" \
  io.hass.type="addon" \
  io.hass.arch="aarch64|amd64|armv7"

CMD [ "/run.sh" ]
