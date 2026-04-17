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
    # Always update config.yaml from the image so deploys take effect.
    # If config changed, also clear stale sessions to avoid 401 errors from
    # sessions created with a previous (possibly broken) config.
    if ! cmp -s "$INIT_DIR/config.yaml" "$DATA_DIR/config.yaml"; then
        echo "[hermes-init] Config changed — updating config.yaml and clearing sessions..."
        cp "$INIT_DIR/config.yaml" "$DATA_DIR/config.yaml"
        rm -f "$DATA_DIR/sessions/session_"*.json
        echo '{}' > "$DATA_DIR/sessions/sessions.json"
        echo "[hermes-init] Config updated, sessions cleared."
    fi
    # Always sync skills and SOUL.md so updates take effect on redeploy
    cp "$INIT_DIR/SOUL.md" "$DATA_DIR/SOUL.md"
    cp -r "$INIT_DIR/skills/." "$DATA_DIR/skills/" 2>/dev/null || true
fi

# Hand off to the real Hermes entrypoint
exec /opt/hermes/docker/entrypoint.sh gateway run
