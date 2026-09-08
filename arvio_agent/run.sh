#!/usr/bin/with-contenv bashio
# with-contenv is required so SUPERVISOR_TOKEN is visible to the process.
set -e
bashio::log.info "Starting Arvio Agent"
exec python3 /app/agent.py
