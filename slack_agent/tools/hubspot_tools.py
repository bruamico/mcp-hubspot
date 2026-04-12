"""
HubSpot tool definitions for the Claude agent.
Uses hubspot-api-client directly — no dependency on mcp_server_hubspot,
which would pull in the MCP SDK, sentence-transformers and FAISS.
"""
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Minimal direct HubSpot client
# ---------------------------------------------------------------------------

class HubSpotDirectClient:
    """Thin wrapper around hubspot-api-client for Slack agent use."""

    def __init__(self, access_token: str):
        from hubspot import HubSpot
        self.hs = HubSpot(access_token=access_token)

    # ── Contacts ──────────────────────────────────────────────────────────

    def get_recent_contacts(self, limit: int = 10) -> str:
        from hubspot.crm.contacts import PublicObjectSearchRequest
        req = PublicObjectSearchRequest(
            sorts=[{"propertyName": "lastmodifieddate", "direction": "DESCENDING"}],
            limit=limit,
        )
        res = self.hs.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        return json.dumps([c.to_dict() for c in res.results], default=str)

    def get_contact_by_id(self, contact_id: str, properties: Optional[list] = None) -> str:
        kwargs = {}
        if properties:
            kwargs["properties"] = properties
        contact = self.hs.crm.contacts.basic_api.get_by_id(
            contact_id=contact_id, **kwargs
        )
        return json.dumps(contact.to_dict(), default=str)

    def create_contact(self, firstname: str, lastname: str, email: Optional[str] = None,
                       extra_properties: Optional[dict] = None) -> str:
        from hubspot.crm.contacts import PublicObjectSearchRequest, SimplePublicObjectInputForCreate
        # Duplicate check
        filters = [
            {"propertyName": "firstname", "operator": "EQ", "value": firstname},
            {"propertyName": "lastname", "operator": "EQ", "value": lastname},
        ]
        search_res = self.hs.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[{"filters": filters}]
            )
        )
        if search_res.total > 0:
            return json.dumps({"status": "duplicate", "existing": search_res.results[0].to_dict()}, default=str)

        props = {"firstname": firstname, "lastname": lastname}
        if email:
            props["email"] = email
        if extra_properties:
            props.update(extra_properties)
        result = self.hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        return json.dumps({"status": "created", "contact": result.to_dict()}, default=str)

    def update_contact(self, contact_id: str, properties: dict) -> str:
        from hubspot.crm.contacts import SimplePublicObjectInput
        result = self.hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=properties),
        )
        return json.dumps(result.to_dict(), default=str)

    # ── Companies ─────────────────────────────────────────────────────────

    def get_recent_companies(self, limit: int = 10) -> str:
        from hubspot.crm.companies import PublicObjectSearchRequest
        req = PublicObjectSearchRequest(
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
            limit=limit,
        )
        res = self.hs.crm.companies.search_api.do_search(
            public_object_search_request=req
        )
        return json.dumps([c.to_dict() for c in res.results], default=str)

    def get_company_by_id(self, company_id: str, properties: Optional[list] = None) -> str:
        kwargs = {}
        if properties:
            kwargs["properties"] = properties
        company = self.hs.crm.companies.basic_api.get_by_id(
            company_id=company_id, **kwargs
        )
        return json.dumps(company.to_dict(), default=str)

    def get_company_activity(self, company_id: str) -> str:
        """Get associations (deals, contacts, tickets) for a company."""
        assoc = self.hs.crm.companies.associations_api.get_all(
            company_id=company_id, to_object_type="contacts"
        )
        return json.dumps({"company_id": company_id, "associations": [a.to_dict() for a in assoc.results]}, default=str)

    def create_company(self, name: str, extra_properties: Optional[dict] = None) -> str:
        from hubspot.crm.companies import PublicObjectSearchRequest, SimplePublicObjectInputForCreate
        search_res = self.hs.crm.companies.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[{"filters": [{"propertyName": "name", "operator": "EQ", "value": name}]}]
            )
        )
        if search_res.total > 0:
            return json.dumps({"status": "duplicate", "existing": search_res.results[0].to_dict()}, default=str)

        props = {"name": name}
        if extra_properties:
            props.update(extra_properties)
        result = self.hs.crm.companies.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        return json.dumps({"status": "created", "company": result.to_dict()}, default=str)

    def update_company(self, company_id: str, properties: dict) -> str:
        from hubspot.crm.companies import SimplePublicObjectInput
        result = self.hs.crm.companies.basic_api.update(
            company_id=company_id,
            simple_public_object_input=SimplePublicObjectInput(properties=properties),
        )
        return json.dumps(result.to_dict(), default=str)

    # ── Company search by name ────────────────────────────────────────────

    def search_companies_by_name(self, name: str, limit: int = 5) -> str:
        from hubspot.crm.companies import PublicObjectSearchRequest
        res = self.hs.crm.companies.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[{"filters": [
                    {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": name}
                ]}],
                properties=["name", "domain", "hs_lastmodifieddate"],
                limit=limit,
            )
        )
        return json.dumps({"total": res.total, "results": [c.to_dict() for c in res.results]}, default=str)

    # ── Contact search by name or email ─────────────────────────────────────

    def search_contacts_by_name(self, name: str, limit: int = 5) -> str:
        from hubspot.crm.contacts import PublicObjectSearchRequest
        # Try by full name first, then fallback to firstname CONTAINS
        parts = name.strip().split(" ", 1)
        filters = []
        if len(parts) == 2:
            filters = [
                {"propertyName": "firstname", "operator": "CONTAINS_TOKEN", "value": parts[0]},
                {"propertyName": "lastname", "operator": "CONTAINS_TOKEN", "value": parts[1]},
            ]
        else:
            filters = [{"propertyName": "firstname", "operator": "CONTAINS_TOKEN", "value": name}]
        res = self.hs.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[{"filters": filters}],
                properties=["firstname", "lastname", "email", "company", "hs_lastmodifieddate", "lifecyclestage"],
                limit=limit,
            )
        )
        if res.total == 0 and len(parts) == 2:
            # Retry with just first name
            res2 = self.hs.crm.contacts.search_api.do_search(
                public_object_search_request=PublicObjectSearchRequest(
                    filter_groups=[{"filters": [
                        {"propertyName": "firstname", "operator": "CONTAINS_TOKEN", "value": parts[0]}
                    ]}],
                    properties=["firstname", "lastname", "email", "company", "hs_lastmodifieddate", "lifecyclestage"],
                    limit=limit,
                )
            )
            res = res2
        return json.dumps({"total": res.total, "results": [c.to_dict() for c in res.results]}, default=str)

    # ── Company timeline (engagements) ────────────────────────────────────

    def get_company_timeline(self, company_id: str, limit: int = 20) -> str:
        """Fetch recent engagements + CRM activities (meetings, calls) for a company."""
        import urllib.request as _req
        import json as _json
        token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        results = []

        def _get(url: str) -> dict:
            r = _req.Request(url, headers=headers)
            with _req.urlopen(r, timeout=15) as resp:
                return _json.loads(resp.read())

        def _post(url: str, body: dict) -> dict:
            data = _json.dumps(body).encode()
            r = _req.Request(url, data=data, headers=headers, method="POST")
            with _req.urlopen(r, timeout=15) as resp:
                return _json.loads(resp.read())

        # 1. Old engagements API (pre-2022 emails, notes, calls)
        try:
            data = _get(
                f"https://api.hubapi.com/engagements/v1/engagements/associated/COMPANY/"
                f"{company_id}/paged?limit={limit}&count={limit}"
            )
            for eng in data.get("results", []):
                e = eng.get("engagement", {})
                m = eng.get("metadata", {})
                results.append({
                    "type": e.get("type"),
                    "date": e.get("createdAt"),
                    "subject": m.get("subject") or m.get("title") or "",
                    "body": (m.get("body") or m.get("text") or "")[:300],
                })
        except Exception as exc:
            logger.warning("Engagements v1 API: %s", exc)

        # 2. New CRM objects (v3) — meetings, calls, notes
        for obj_type, props in [
            ("meetings", [
                "hs_meeting_title", "hs_meeting_start_time", "hs_meeting_body",
                "hs_meeting_outcome", "hs_internal_meeting_notes",
            ]),
            ("calls", [
                "hs_call_title", "hs_call_direction", "hs_call_duration",
                "hs_call_body", "hs_call_status", "hs_timestamp",
            ]),
            ("notes", [
                "hs_note_body", "hs_timestamp", "hs_lastmodifieddate",
            ]),
        ]:
            try:
                assoc = _get(
                    f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}"
                    f"/associations/{obj_type}?limit=20"
                )
                ids = [r["id"] for r in assoc.get("results", [])[:15]]
                if not ids:
                    continue
                batch = _post(
                    f"https://api.hubapi.com/crm/v3/objects/{obj_type}/batch/read",
                    {"inputs": [{"id": i} for i in ids], "properties": props},
                )
                for item in batch.get("results", []):
                    p = item.get("properties", {})
                    if obj_type == "meetings":
                        body = (p.get("hs_meeting_body") or "")[:800]
                        internal_notes = (p.get("hs_internal_meeting_notes") or "")[:400]
                        results.append({
                            "type": "MEETING",
                            "date": p.get("hs_meeting_start_time"),
                            "subject": p.get("hs_meeting_title") or "",
                            "body": body,
                            "internal_notes": internal_notes,
                            "outcome": p.get("hs_meeting_outcome") or "",
                        })
                    elif obj_type == "calls":
                        results.append({
                            "type": "CALL",
                            "date": p.get("hs_timestamp"),
                            "subject": p.get("hs_call_title") or "",
                            "body": (p.get("hs_call_body") or "")[:800],
                            "status": p.get("hs_call_status") or "",
                            "duration_ms": p.get("hs_call_duration"),
                        })
                    else:  # notes
                        note_body = (p.get("hs_note_body") or "")[:600]
                        if note_body:
                            results.append({
                                "type": "NOTE",
                                "date": p.get("hs_timestamp") or p.get("hs_lastmodifieddate"),
                                "subject": "",
                                "body": note_body,
                            })
            except Exception as exc:
                logger.warning("CRM %s API for company %s: %s", obj_type, company_id, exc)

        # Sort by date descending, most recent first
        def _ts(r):
            d = r.get("date") or ""
            return str(d)

        results.sort(key=_ts, reverse=True)
        return json.dumps(
            {"company_id": company_id, "total": len(results), "engagements": results[:limit]},
            default=str,
        )

    # ── Contact timeline (engagements) ───────────────────────────────────

    def get_contact_timeline(self, contact_id: str, limit: int = 20) -> str:
        """Fetch meetings, calls, and notes associated with a contact."""
        import urllib.request as _req
        import json as _json
        token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        results = []

        def _get(url: str) -> dict:
            r = _req.Request(url, headers=headers)
            with _req.urlopen(r, timeout=15) as resp:
                return _json.loads(resp.read())

        def _post(url: str, body: dict) -> dict:
            data = _json.dumps(body).encode()
            r = _req.Request(url, data=data, headers=headers, method="POST")
            with _req.urlopen(r, timeout=15) as resp:
                return _json.loads(resp.read())

        # 1. Old engagements API
        try:
            data = _get(
                f"https://api.hubapi.com/engagements/v1/engagements/associated/CONTACT/"
                f"{contact_id}/paged?limit={limit}&count={limit}"
            )
            for eng in data.get("results", []):
                e = eng.get("engagement", {})
                m = eng.get("metadata", {})
                results.append({
                    "type": e.get("type"),
                    "date": e.get("createdAt"),
                    "subject": m.get("subject") or m.get("title") or "",
                    "body": (m.get("body") or m.get("text") or "")[:300],
                })
        except Exception as exc:
            logger.warning("Engagements v1 API (contact): %s", exc)

        # 2. CRM v3 meetings, calls, notes associated with the contact
        for obj_type, props in [
            ("meetings", [
                "hs_meeting_title", "hs_meeting_start_time", "hs_meeting_body",
                "hs_meeting_outcome", "hs_internal_meeting_notes",
            ]),
            ("calls", [
                "hs_call_title", "hs_call_direction", "hs_call_duration",
                "hs_call_body", "hs_call_status", "hs_timestamp",
            ]),
            ("notes", [
                "hs_note_body", "hs_timestamp", "hs_lastmodifieddate",
            ]),
        ]:
            try:
                assoc = _get(
                    f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}"
                    f"/associations/{obj_type}?limit=20"
                )
                ids = [r["id"] for r in assoc.get("results", [])[:15]]
                if not ids:
                    continue
                batch = _post(
                    f"https://api.hubapi.com/crm/v3/objects/{obj_type}/batch/read",
                    {"inputs": [{"id": i} for i in ids], "properties": props},
                )
                for item in batch.get("results", []):
                    p = item.get("properties", {})
                    if obj_type == "meetings":
                        results.append({
                            "type": "MEETING",
                            "date": p.get("hs_meeting_start_time"),
                            "subject": p.get("hs_meeting_title") or "",
                            "body": (p.get("hs_meeting_body") or "")[:800],
                            "internal_notes": (p.get("hs_internal_meeting_notes") or "")[:400],
                            "outcome": p.get("hs_meeting_outcome") or "",
                        })
                    elif obj_type == "calls":
                        results.append({
                            "type": "CALL",
                            "date": p.get("hs_timestamp"),
                            "subject": p.get("hs_call_title") or "",
                            "body": (p.get("hs_call_body") or "")[:800],
                            "status": p.get("hs_call_status") or "",
                            "duration_ms": p.get("hs_call_duration"),
                        })
                    else:  # notes
                        note_body = (p.get("hs_note_body") or "")[:600]
                        if note_body:
                            results.append({
                                "type": "NOTE",
                                "date": p.get("hs_timestamp") or p.get("hs_lastmodifieddate"),
                                "subject": "",
                                "body": note_body,
                            })
            except Exception as exc:
                logger.warning("CRM %s API for contact %s: %s", obj_type, contact_id, exc)

        def _ts(r):
            d = r.get("date") or ""
            return str(d)

        results.sort(key=_ts, reverse=True)
        return json.dumps(
            {"contact_id": contact_id, "total": len(results), "engagements": results[:limit]},
            default=str,
        )

    # ── Tickets ───────────────────────────────────────────────────────────

    def get_tickets(self, criteria: str = "default", limit: int = 20) -> str:
        from hubspot.crm.tickets import PublicObjectSearchRequest
        filters = []
        if criteria == "Closed":
            filters = [{"propertyName": "hs_pipeline_stage", "operator": "EQ", "value": "4"}]

        req = PublicObjectSearchRequest(
            filter_groups=[{"filters": filters}] if filters else [],
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
            properties=["subject", "hs_pipeline_stage", "hs_ticket_priority",
                        "hs_lastmodifieddate", "hs_ticket_id"],
            limit=limit,
        )
        res = self.hs.crm.tickets.search_api.do_search(
            public_object_search_request=req
        )
        return json.dumps({"total": res.total, "results": [t.to_dict() for t in res.results]}, default=str)

    def get_ticket_threads(self, ticket_id: str) -> str:
        """Get conversation threads associated with a ticket via conversations API."""
        import urllib.request
        token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN", "")
        url = f"https://api.hubapi.com/crm/v3/objects/tickets/{ticket_id}/associations/conversations"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            return json.dumps(data, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc), "ticket_id": ticket_id})

    # ── Conversations ─────────────────────────────────────────────────────

    def get_recent_conversations(self, limit: int = 10) -> str:
        import urllib.request
        token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN", "")
        url = f"https://api.hubapi.com/conversations/v3/conversations/threads?limit={limit}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode()
        except Exception as exc:
            return json.dumps({"error": str(exc)})


# Singleton
_client: Optional[HubSpotDirectClient] = None


def _hs_client() -> HubSpotDirectClient:
    global _client
    if _client is None:
        token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_TOKEN")
        if not token:
            raise ValueError("HUBSPOT_ACCESS_TOKEN or HUBSPOT_TOKEN env var required")
        _client = HubSpotDirectClient(access_token=token)
    return _client


# ---------------------------------------------------------------------------
# Tool definitions (JSON schema for Claude)
# ---------------------------------------------------------------------------

HUBSPOT_TOOL_DEFINITIONS = [
    {
        "name": "hubspot_get_active_contacts",
        "description": "Busca os contatos mais recentemente ativos no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Número máximo de contatos (padrão: 10)"}
            },
        },
    },
    {
        "name": "hubspot_get_contact",
        "description": "Busca um contato específico pelo seu ID no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "ID do contato no HubSpot"},
                "properties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista opcional de propriedades a retornar",
                },
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "hubspot_create_contact",
        "description": "Cria um novo contato no HubSpot (com verificação de duplicata).",
        "input_schema": {
            "type": "object",
            "properties": {
                "firstname": {"type": "string", "description": "Primeiro nome"},
                "lastname": {"type": "string", "description": "Sobrenome"},
                "email": {"type": "string", "description": "E-mail"},
                "properties": {"type": "object", "description": "Propriedades adicionais"},
            },
            "required": ["firstname", "lastname"],
        },
    },
    {
        "name": "hubspot_update_contact",
        "description": "Atualiza propriedades de um contato existente no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "ID do contato"},
                "properties": {"type": "object", "description": "Propriedades a atualizar"},
            },
            "required": ["contact_id", "properties"],
        },
    },
    {
        "name": "hubspot_get_active_companies",
        "description": "Busca as empresas mais recentemente ativas no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Número máximo de empresas (padrão: 10)"}
            },
        },
    },
    {
        "name": "hubspot_get_company",
        "description": "Busca uma empresa específica pelo seu ID no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "ID da empresa"},
                "properties": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lista opcional de propriedades a retornar",
                },
            },
            "required": ["company_id"],
        },
    },
    {
        "name": "hubspot_get_company_activity",
        "description": "Retorna associações (contatos, deals, tickets) de uma empresa no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "ID da empresa"}
            },
            "required": ["company_id"],
        },
    },
    {
        "name": "hubspot_search_company_by_name",
        "description": "Busca empresas no HubSpot pelo nome. Útil para correlacionar clientes Slack com companies do CRM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome ou parte do nome da empresa"},
                "limit": {"type": "integer", "description": "Número máximo de resultados (padrão: 5)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "hubspot_get_company_timeline",
        "description": (
            "Retorna a timeline de engajamentos (emails, calls, notas, reuniões) de uma empresa no HubSpot. "
            "Inclui resumos de calls do Read.ai sincronizados ao CRM."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "ID da empresa no HubSpot"},
                "limit": {"type": "integer", "description": "Número máximo de engajamentos (padrão: 20)"},
            },
            "required": ["company_id"],
        },
    },
    {
        "name": "hubspot_create_company",
        "description": "Cria uma nova empresa no HubSpot (com verificação de duplicata).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome da empresa"},
                "properties": {"type": "object", "description": "Propriedades adicionais"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "hubspot_update_company",
        "description": "Atualiza propriedades de uma empresa existente no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "string", "description": "ID da empresa"},
                "properties": {"type": "object", "description": "Propriedades a atualizar"},
            },
            "required": ["company_id", "properties"],
        },
    },
    {
        "name": "hubspot_get_tickets",
        "description": "Busca tickets no HubSpot. criteria='default' = abertos/recentes, 'Closed' = encerrados.",
        "input_schema": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "string",
                    "enum": ["default", "Closed"],
                    "description": "'default' ou 'Closed'",
                },
                "limit": {"type": "integer", "description": "Número máximo (padrão: 20)"},
            },
        },
    },
    {
        "name": "hubspot_get_ticket_threads",
        "description": "Retorna as threads de conversa de um ticket específico.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "ID do ticket"}
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "hubspot_get_recent_conversations",
        "description": "Busca as conversas/emails recentes no HubSpot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Número máximo (padrão: 10)"}
            },
        },
    },
    {
        "name": "hubspot_search_contact_by_name",
        "description": (
            "Busca contatos no HubSpot pelo nome (ou parte do nome). "
            "Use para localizar um lead ou contato específico antes de buscar sua timeline."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome completo ou parcial do contato"},
                "limit": {"type": "integer", "description": "Número máximo de resultados (padrão: 5)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "hubspot_get_contact_timeline",
        "description": (
            "Retorna a timeline de engajamentos (reuniões, chamadas, notas) de um contato/lead no HubSpot. "
            "Use para buscar notas e atas de reuniões relacionadas a um lead específico. "
            "Para encontrar o contact_id, use hubspot_search_contact_by_name primeiro."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "ID do contato no HubSpot"},
                "limit": {"type": "integer", "description": "Número máximo de engajamentos (padrão: 20)"},
            },
            "required": ["contact_id"],
        },
    },
]

# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

async def execute_hubspot_tool(tool_name: str, tool_input: dict) -> str:
    """Run HubSpot tools in a thread pool to avoid blocking the asyncio event loop.

    All HubSpotDirectClient methods use the synchronous hubspot-api-client / urllib
    under the hood. Calling them directly from a coroutine would freeze the entire
    event loop and cause health-check failures on Fly.io.
    """
    import asyncio
    return await asyncio.to_thread(_execute_hubspot_sync, tool_name, tool_input)


def _execute_hubspot_sync(tool_name: str, tool_input: dict) -> str:
    """Synchronous implementation — called via asyncio.to_thread()."""
    try:
        hs = _hs_client()

        if tool_name == "hubspot_get_active_contacts":
            return hs.get_recent_contacts(limit=int(tool_input.get("limit", 10)))

        elif tool_name == "hubspot_get_contact":
            return hs.get_contact_by_id(tool_input["contact_id"], tool_input.get("properties"))

        elif tool_name == "hubspot_create_contact":
            return hs.create_contact(
                firstname=tool_input["firstname"],
                lastname=tool_input["lastname"],
                email=tool_input.get("email"),
                extra_properties=tool_input.get("properties"),
            )

        elif tool_name == "hubspot_update_contact":
            return hs.update_contact(tool_input["contact_id"], tool_input["properties"])

        elif tool_name == "hubspot_get_active_companies":
            return hs.get_recent_companies(limit=int(tool_input.get("limit", 10)))

        elif tool_name == "hubspot_get_company":
            return hs.get_company_by_id(tool_input["company_id"], tool_input.get("properties"))

        elif tool_name == "hubspot_get_company_activity":
            return hs.get_company_activity(tool_input["company_id"])

        elif tool_name == "hubspot_create_company":
            return hs.create_company(tool_input["name"], tool_input.get("properties"))

        elif tool_name == "hubspot_update_company":
            return hs.update_company(tool_input["company_id"], tool_input["properties"])

        elif tool_name == "hubspot_get_tickets":
            return hs.get_tickets(
                criteria=tool_input.get("criteria", "default"),
                limit=int(tool_input.get("limit", 20)),
            )

        elif tool_name == "hubspot_get_ticket_threads":
            return hs.get_ticket_threads(tool_input["ticket_id"])

        elif tool_name == "hubspot_get_recent_conversations":
            return hs.get_recent_conversations(limit=int(tool_input.get("limit", 10)))

        elif tool_name == "hubspot_search_company_by_name":
            return hs.search_companies_by_name(tool_input["name"], int(tool_input.get("limit", 5)))

        elif tool_name == "hubspot_get_company_timeline":
            return hs.get_company_timeline(tool_input["company_id"], int(tool_input.get("limit", 20)))

        elif tool_name == "hubspot_search_contact_by_name":
            return hs.search_contacts_by_name(tool_input["name"], int(tool_input.get("limit", 5)))

        elif tool_name == "hubspot_get_contact_timeline":
            return hs.get_contact_timeline(tool_input["contact_id"], int(tool_input.get("limit", 20)))

        else:
            return f"Ferramenta desconhecida: {tool_name}"

    except Exception as exc:
        logger.error("HubSpot tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"
