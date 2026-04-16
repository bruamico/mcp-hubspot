import os
import json
import yaml
import logging
import httpx
from pathlib import Path
from openai import OpenAI
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("hermes")

CONFIG_PATH = os.environ.get("HERMES_CONFIG", "/app/config.yaml")
SOUL_PATH = os.environ.get("HERMES_SOUL", "/app/SOUL.md")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_soul() -> str:
    with open(SOUL_PATH) as f:
        return f.read()


class Memory:
    def __init__(self, path: str, limit: int = 50):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.history_file = self.path / "conversations.json"
        self.limit = limit
        self._data = self._load()

    def _load(self) -> dict:
        if self.history_file.exists():
            try:
                with open(self.history_file) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        with open(self.history_file, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def get_history(self, thread_id: str) -> list:
        return self._data.get(thread_id, [])[-self.limit:]

    def add(self, thread_id: str, role: str, content: str) -> None:
        if thread_id not in self._data:
            self._data[thread_id] = []
        self._data[thread_id].append({"role": role, "content": content})
        self._save()


# --- HubSpot skills ---

def get_company_context(company_name: str, days_back: int = 30) -> str:
    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        return "Erro: HUBSPOT_TOKEN não configurado."
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base_url = "https://api.hubapi.com"

    search_body = {
        "filterGroups": [{"filters": [{"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": company_name}]}],
        "properties": ["name", "domain"],
        "limit": 1,
    }
    res = httpx.post(f"{base_url}/crm/v3/objects/companies/search", headers=headers, json=search_body, timeout=15)
    companies = res.json().get("results", [])
    if not companies:
        return f"Nenhuma company encontrada para '{company_name}'."

    company_id = companies[0]["id"]
    real_name = companies[0]["properties"].get("name", company_name)

    deals_body = {
        "filterGroups": [{"filters": [{"propertyName": "associations.company", "operator": "EQ", "value": company_id}]}],
        "properties": ["dealname", "dealstage", "amount", "closedate"],
        "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
        "limit": 5,
    }
    deals_res = httpx.post(f"{base_url}/crm/v3/objects/deals/search", headers=headers, json=deals_body, timeout=15)
    deals = deals_res.json().get("results", [])

    output = f"Contexto HubSpot — {real_name} (ID: {company_id}):\n\n"
    if deals:
        output += "Deals Recentes:\n"
        for d in deals:
            p = d.get("properties", {})
            output += f"- {p.get('dealname')} | Estágio: {p.get('dealstage')} | Valor: R$ {p.get('amount', 'N/A')}\n"
    else:
        output += "Nenhum deal recente encontrado.\n"
    return output


def create_hubspot_ticket(subject: str, content: str, company_name: str = None) -> str:
    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        return "Erro: HUBSPOT_TOKEN não configurado."
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base_url = "https://api.hubapi.com"

    company_id = None
    if company_name:
        search_body = {
            "filterGroups": [{"filters": [{"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": company_name}]}],
            "limit": 1,
        }
        res = httpx.post(f"{base_url}/crm/v3/objects/companies/search", headers=headers, json=search_body, timeout=15)
        results = res.json().get("results", [])
        if results:
            company_id = results[0]["id"]

    properties = {"subject": subject, "content": content, "hs_pipeline": "0", "hs_pipeline_stage": "1"}
    body = {"properties": properties}
    if company_id:
        body["associations"] = [
            {"to": {"id": company_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 339}]}
        ]

    res = httpx.post(f"{base_url}/crm/v3/objects/tickets", headers=headers, json=body, timeout=15)
    if res.status_code in (200, 201):
        return f"Ticket criado com sucesso! ID: {res.json().get('id')}"
    return f"Erro ao criar ticket: {res.text}"


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_company_context",
            "description": "Busca contexto de um cliente no HubSpot: deals recentes, estágio e valor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {"type": "string", "description": "Nome ou slug do cliente (ex: 'galena', 'sympla')"},
                    "days_back": {"type": "integer", "description": "Dias de histórico. Padrão: 30"},
                },
                "required": ["company_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_hubspot_ticket",
            "description": "Cria um ticket no Service Hub do HubSpot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Título do ticket"},
                    "content": {"type": "string", "description": "Descrição detalhada"},
                    "company_name": {"type": "string", "description": "Nome do cliente (opcional)"},
                },
                "required": ["subject", "content"],
            },
        },
    },
]

TOOL_REGISTRY = {
    "get_company_context": get_company_context,
    "create_hubspot_ticket": create_hubspot_ticket,
}


def call_tool(name: str, arguments: dict) -> str:
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return f"Ferramenta '{name}' não encontrada."
    return fn(**arguments)


def chat(openai_client: OpenAI, config: dict, soul: str, memory: Memory, thread_id: str, user_text: str) -> str:
    memory.add(thread_id, "user", user_text)
    history = memory.get_history(thread_id)
    messages = [{"role": "system", "content": soul}] + history

    for _ in range(10):  # agentic loop, max 10 iterations
        response = openai_client.chat.completions.create(
            model=config["agent"]["model"],
            temperature=config["agent"]["temperature"],
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append(msg)
            for tc in msg.tool_calls:
                result = call_tool(tc.function.name, json.loads(tc.function.arguments))
                logger.info("Tool %s → %s", tc.function.name, result[:120])
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        else:
            reply = msg.content or ""
            memory.add(thread_id, "assistant", reply)
            return reply

    return "Não consegui completar a tarefa após várias tentativas."


def main():
    config = load_config()
    soul = load_soul()
    memory = Memory(config["memory"]["path"], limit=50)
    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    app = App(token=os.environ["TROPICAL_BOT_TOKEN"])

    def handle(event, say):
        text = event.get("text", "").strip()
        thread_ts = event.get("thread_ts") or event["ts"]
        channel = event["channel"]

        # Strip bot mention from text
        if "<@" in text:
            bot_id = app.client.auth_test()["user_id"]
            text = text.replace(f"<@{bot_id}>", "").strip()

        if not text:
            return

        logger.info("Message in %s [thread=%s]: %s", channel, thread_ts, text[:80])
        try:
            reply = chat(openai_client, config, soul, memory, thread_ts, text)
            say(text=reply, thread_ts=thread_ts)
        except Exception as e:
            logger.exception("Error processing message")
            say(text=f"Ocorreu um erro interno: {e}", thread_ts=thread_ts)

    @app.event("app_mention")
    def on_mention(event, say):
        handle(event, say)

    @app.event("message")
    def on_message(event, say):
        if event.get("channel_type") != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        handle(event, say)

    handler = SocketModeHandler(app, os.environ["TROPICAL_APP_TOKEN"])
    logger.info("Hermes Agent online via Slack Socket Mode")
    handler.start()


if __name__ == "__main__":
    main()
