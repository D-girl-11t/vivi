#!/bin/sh
# Start Bring-up Studio on http://127.0.0.1:8765  (no installs needed: python3 + pyyaml + jinja2)
cd "$(dirname "$0")" && exec python3 -m server.app "$@"
