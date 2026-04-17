FROM nousresearch/hermes-agent:latest

# Copy our custom config to a staging area inside the image
# (not the volume — the entrypoint copies them to /opt/data on first run)
COPY config.yaml /opt/hermes-init/config.yaml
COPY SOUL.md /opt/hermes-init/SOUL.md
COPY skills/ /opt/hermes-init/skills/
COPY docker-entrypoint.sh /opt/hermes-init/entrypoint.sh

RUN chmod +x /opt/hermes-init/entrypoint.sh

ENTRYPOINT ["/opt/hermes-init/entrypoint.sh"]
