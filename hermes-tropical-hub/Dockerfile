FROM ghcr.io/nousresearch/hermes-agent:latest

WORKDIR /app

# Copy agent configuration and identity
COPY SOUL.md /app/SOUL.md
COPY config.yaml /app/config.yaml

# Copy skills
COPY skills/ /app/skills/
