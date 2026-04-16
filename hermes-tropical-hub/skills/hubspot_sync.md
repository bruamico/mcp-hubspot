# Skill: HubSpot — Contexto de Clientes e Tickets

## Descrição
Esta skill permite que o Hermes acesse o HubSpot da Tropical Hub (Portal ID: 1858913)
para buscar contexto de clientes e criar tickets no Service Hub.
Usa a Service Key (HUBSPOT_TOKEN) para acesso completo de leitura e escrita.

## Tool 1: get_company_context

**Descrição:** Busca o contexto mais recente de um cliente no HubSpot,
combinando timeline de notas/calls e deals recentes.

**Parâmetros:**
- company_name (string): Nome ou slug do cliente (ex: "galena", "haytek")
- days_back (integer, opcional): Dias de histórico. Padrão: 30.

**Código Python:**

import os, httpx

def get_company_context(company_name: str, days_back: int = 30) -> str:
    token = os.environ.get("HUBSPOT_TOKEN")
    if not token:
        return "Erro: HUBSPOT_TOKEN não configurado."
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base_url = "https://api.hubapi.com"
    search_body = {
        "filterGroups": [{"filters": [{"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": company_name}]}],
        "properties": ["name", "domain"],
        "limit": 1
    }
    res = httpx.post(f"{base_url}/crm/v3/objects/companies/search", headers=headers, json=search_body)
    companies = res.json().get("results", [])
    if not companies:
        return f"Nenhuma company encontrada para '{company_name}'."
    company_id = companies[0]["id"]
    real_name = companies[0]["properties"].get("name", company_name)
    deals_body = {
        "filterGroups": [{"filters": [{"propertyName": "associations.company", "operator": "EQ", "value": company_id}]}],
        "properties": ["dealname", "dealstage", "amount", "closedate"],
        "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
        "limit": 5
    }
    deals_res = httpx.post(f"{base_url}/crm/v3/objects/deals/search", headers=headers, json=deals_body)
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


## Tool 2: create_hubspot_ticket

**Descrição:** Cria um ticket no Service Hub do HubSpot para um cliente.

**Parâmetros:**
- subject (string): Assunto ou título do ticket.
- content (string): Descrição detalhada do problema ou solicitação.
- company_name (string, opcional): Nome do cliente para associar o ticket.

**Código Python:**

import os, httpx

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
            "limit": 1
        }
        res = httpx.post(f"{base_url}/crm/v3/objects/companies/search", headers=headers, json=search_body)
        companies = res.json().get("results", [])
        if companies:
            company_id = companies[0]["id"]
    properties = {"subject": subject, "content": content, "hs_pipeline": "0", "hs_pipeline_stage": "1"}
    body = {"properties": properties}
    if company_id:
        body["associations"] = [{"to": {"id": company_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 339}]}]
    res = httpx.post(f"{base_url}/crm/v3/objects/tickets", headers=headers, json=body)
    if res.status_code in (200, 201):
        return f"Ticket criado com sucesso! ID: {res.json().get('id')}"
    else:
        return f"Erro ao criar ticket: {res.text}"
