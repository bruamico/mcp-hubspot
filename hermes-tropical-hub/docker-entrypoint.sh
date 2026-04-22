#!/bin/bash
set -e

DATA_DIR="/opt/data"
INIT_DIR="/opt/hermes-init"

# On first run, create directory structure
if [ ! -f "$DATA_DIR/config.yaml" ]; then
    echo "[hermes-init] First run detected — initializing $DATA_DIR..."
    mkdir -p "$DATA_DIR/skills" "$DATA_DIR/memories" "$DATA_DIR/sessions"
fi

# Always sync config.yaml, SOUL.md and skills so updates take effect on redeploy
cp "$INIT_DIR/config.yaml" "$DATA_DIR/config.yaml"
cp "$INIT_DIR/SOUL.md" "$DATA_DIR/SOUL.md"
cp -r "$INIT_DIR/skills/." "$DATA_DIR/skills/" 2>/dev/null || true
echo "[hermes-init] Config, SOUL.md and skills synced."

# Patch _try_anthropic() to hard-block Anthropic calls unless explicitly configured.
# This prevents 401 errors when OpenRouter is the configured provider.
AUX_CLIENT="/opt/hermes/agent/auxiliary_client.py"
if [ -f "$AUX_CLIENT" ] && ! grep -q "HARD BLOCK" "$AUX_CLIENT"; then
    python3 - <<'PYEOF'
import sys
filepath = '/opt/hermes/agent/auxiliary_client.py'
with open(filepath, 'r') as f:
    content = f.read()
old = ('def _try_anthropic() -> Tuple[Optional[Any], Optional[str]]:\n'
       '    try:\n'
       '        from agent.anthropic_adapter import build_anthropic_client, resolve_anthropic_token\n'
       '    except ImportError:\n'
       '        return None, None')
new = ('def _try_anthropic() -> Tuple[Optional[Any], Optional[str]]:\n'
       '    # HARD BLOCK: never call Anthropic unless explicitly configured as provider\n'
       '    try:\n'
       '        from hermes_cli.auth import is_provider_explicitly_configured\n'
       '        if not is_provider_explicitly_configured("anthropic"):\n'
       '            return None, None\n'
       '    except Exception:\n'
       '        return None, None\n'
       '    try:\n'
       '        from agent.anthropic_adapter import build_anthropic_client, resolve_anthropic_token\n'
       '    except ImportError:\n'
       '        return None, None')
if old in content:
    with open(filepath, 'w') as f:
        f.write(content.replace(old, new, 1))
    import glob, os
    for pyc in glob.glob('/opt/hermes/agent/__pycache__/auxiliary_client*.pyc'):
        os.remove(pyc)
    print("[hermes-init] Patched _try_anthropic() — Anthropic hard-blocked.")
else:
    print("[hermes-init] _try_anthropic() patch: pattern not found (already patched or version changed).")
PYEOF
fi

# Hand off to the real Hermes entrypoint
exec /opt/hermes/docker/entrypoint.sh gateway run
