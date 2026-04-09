# mcp-hubspot

HubSpot MCP Server + Slack Agent powered by Claude API.

## Components

- **MCP Server** (`src/mcp_server_hubspot/`): MCP server with 18 HubSpot tools for Claude Desktop
- **Slack Agent** (`slack_agent/`): Slack bot with Claude API agent loop, HubSpot + Read.ai integration

## Quick start

```bash
pip install -e ".[slack]"
slack-agent
```
