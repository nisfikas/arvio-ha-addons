#!/usr/bin/with-contenv bashio
set -e
bashio::log.info "Starting Arvio Updater"
exec python3 /app/updater.py
