#!/bin/bash
set -e

DATA_DIR="/opt/data"
INIT_DIR="/opt/hermes-init"

# On first run, populate /opt/data with our custom config
if [ ! -f "$DATA_DIR/config.yaml" ]; then
    echo "[hermes-init] First run detected — initializing $DATA_DIR..."
    mkdir -p "$DATA_DIR/skills" "$DATA_DIR/memories" "$DATA_DIR/sessions"
    cp "$INIT_DIR/config.yaml" "$DATA_DIR/config.yaml"
    cp "$INIT_DIR/SOUL.md" "$DATA_DIR/SOUL.md"
    cp -r "$INIT_DIR/skills/." "$DATA_DIR/skills/" 2>/dev/null || true
    echo "[hermes-init] Done."
else
    # Always sync skills and SOUL.md so updates take effect on redeploy
    cp "$INIT_DIR/SOUL.md" "$DATA_DIR/SOUL.md"
    cp -r "$INIT_DIR/skills/." "$DATA_DIR/skills/" 2>/dev/null || true
fi

# Hand off to the real Hermes entrypoint
exec /opt/hermes/docker/entrypoint.sh gateway run
