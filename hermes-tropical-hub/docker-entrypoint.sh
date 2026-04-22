#!/bin/bash
set -e

DATA_DIR="/opt/data"
INIT_DIR="/opt/hermes-init"

# Strip credentials that could trigger accidental Anthropic direct-calls.
# OpenRouter credentials live in auth.json; these env vars are not needed.
unset OPENAI_API_KEY
unset ANTHROPIC_API_KEY
unset ANTHROPIC_TOKEN
unset CLAUDE_CODE_OAUTH_TOKEN
export ANTHROPIC_API_KEY=""
export ANTHROPIC_TOKEN=""
echo "[hermes-init] Anthropic/OpenAI env credentials cleared."

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

# Patch run_agent.py to never enter anthropic_messages mode when provider is an aggregator.
# Without this, RunAgent may still call api.anthropic.com directly if it detects an
# Anthropic-flavored model name even though the actual route is through OpenRouter.
RUN_AGENT="/opt/hermes/run_agent.py"
if [ -f "$RUN_AGENT" ] && ! grep -q "AGGREGATOR BLOCK" "$RUN_AGENT"; then
    python3 - <<'PYEOF'
import glob, os, re
filepath = '/opt/hermes/run_agent.py'
with open(filepath, 'r') as f:
    content = f.read()

# Pattern: the elif that sets api_mode = "anthropic_messages"
# We inject a guard so aggregator providers (openrouter, etc.) are never rerouted
old = ('            elif self.provider == "anthropic" or '
       '(provider_name is None and "api.anthropic.com" in self._base_url_lower):\n'
       '                self.api_mode = "anthropic_messages"')
new = ('            elif self.provider not in ("openrouter", "kilocode", "ai-gateway", "nous") and '
       '(self.provider == "anthropic" or '  # AGGREGATOR BLOCK
       '(provider_name is None and "api.anthropic.com" in self._base_url_lower)):\n'
       '                self.api_mode = "anthropic_messages"')

if old in content:
    with open(filepath, 'w') as f:
        f.write(content.replace(old, new, 1))
    for pyc in glob.glob('/opt/hermes/__pycache__/run_agent*.pyc'):
        os.remove(pyc)
    print("[hermes-init] Patched run_agent.py — aggregator providers blocked from anthropic_messages mode.")
else:
    print("[hermes-init] run_agent.py patch: pattern not found — checking alternate form...")
    # Try without the tuple form (in case it's slightly different)
    # Just search and report what the nearby code looks like
    idx = content.find('api_mode = "anthropic_messages"')
    if idx >= 0:
        snippet = content[max(0,idx-200):idx+100]
        print(f"[hermes-init] Found anthropic_messages at offset {idx}. Context:\n{snippet}")
    else:
        print("[hermes-init] anthropic_messages string not found in run_agent.py.")
PYEOF
fi

# Hand off to the real Hermes entrypoint
exec /opt/hermes/docker/entrypoint.sh gateway run
