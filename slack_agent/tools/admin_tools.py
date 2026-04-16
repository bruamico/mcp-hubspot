"""
Admin tools — RBAC permission management and dynamic bot configuration.
Restricted to users with 'admin' role (or ADMIN_SLACK_USER_ID env var).
"""
import logging
import os

logger = logging.getLogger(__name__)

ADMIN_TOOL_DEFINITIONS = [
    {
        "name": "manage_permissions",
        "description": (
            "Gerencia permissões de acesso ao bot por usuário Slack. "
            "Apenas administradores podem grant/revoke. "
            "Actions: 'grant' = conceder role, 'revoke' = redefinir para user, 'list' = listar todos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["grant", "revoke", "list"],
                    "description": "grant, revoke ou list",
                },
                "user_id": {
                    "type": "string",
                    "description": "Slack Member ID (ex: U01ABC2DEF3) — obrigatório para grant/revoke",
                },
                "role": {
                    "type": "string",
                    "enum": ["admin", "user"],
                    "description": "Role a conceder (obrigatório para grant)",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_config",
        "description": (
            "Gerencia configurações dinâmicas do bot: chaves de API, URLs, parâmetros de integração. "
            "set/delete exigem role admin. get/list são abertos a qualquer usuário. "
            "Use get_config para recuperar uma chave antes de executar uma tarefa que exija integração externa."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "get", "list", "delete"],
                    "description": "set / get / list / delete",
                },
                "key": {
                    "type": "string",
                    "description": "Chave da configuração (ex: 'zapier_api_key') — obrigatório para set/get/delete",
                },
                "value": {
                    "type": "string",
                    "description": "Valor a armazenar — obrigatório para set",
                },
            },
            "required": ["action"],
        },
    },
]


async def execute_admin_tool(tool_name: str, tool_input: dict, context: dict | None = None) -> str:
    user_id = (context or {}).get("user_id", "")
    try:
        if tool_name == "manage_permissions":
            return await _manage_permissions(tool_input, caller_user_id=user_id)
        elif tool_name == "manage_config":
            return await _manage_config(tool_input, caller_user_id=user_id)
        return f"Ferramenta desconhecida: {tool_name}"
    except Exception as exc:
        logger.error("admin tool %s failed: %s", tool_name, exc)
        return f"Erro ao executar {tool_name}: {exc}"


async def _require_admin(caller_user_id: str) -> str | None:
    """Return an error message if caller is not admin, else None.
    Empty user_id means an internal/scheduler call — always allowed."""
    if not caller_user_id:
        return None
    from ..models.database import is_admin
    if not await is_admin(caller_user_id):
        return "⛔ Acesso negado. Apenas administradores podem realizar esta ação."
    return None


async def _manage_permissions(inp: dict, caller_user_id: str) -> str:
    action = inp.get("action", "")

    err = await _require_admin(caller_user_id)
    if err:
        return err

    from ..models.database import set_user_role, list_user_permissions

    if action == "grant":
        uid = (inp.get("user_id") or "").strip()
        role = (inp.get("role") or "user").strip()
        if not uid:
            return "⚠️ user_id é obrigatório para grant."
        if role not in ("admin", "user"):
            return "⚠️ role deve ser 'admin' ou 'user'."
        await set_user_role(uid, role, granted_by=caller_user_id)
        return f"✅ Permissão concedida: `{uid}` → role `{role}`."

    elif action == "revoke":
        uid = (inp.get("user_id") or "").strip()
        if not uid:
            return "⚠️ user_id é obrigatório para revoke."
        await set_user_role(uid, "user", granted_by=caller_user_id)
        return f"✅ Permissão de `{uid}` redefinida para `user`."

    elif action == "list":
        rows = await list_user_permissions()
        admin_uid = os.getenv("ADMIN_SLACK_USER_ID", "")
        lines = ["*Permissões configuradas:*"]
        if admin_uid:
            lines.append(f"• `{admin_uid}` — admin _(ADMIN_SLACK_USER_ID env var)_")
        for r in rows:
            by = r.get("granted_by") or "N/A"
            lines.append(f"• `{r['slack_user_id']}` — {r['role']} _(concedido por {by})_")
        if not rows and not admin_uid:
            lines.append("Nenhuma permissão configurada — todos têm acesso básico.")
        return "\n".join(lines)

    return f"Ação desconhecida: {action}"


async def _manage_config(inp: dict, caller_user_id: str) -> str:
    action = inp.get("action", "")
    key    = (inp.get("key") or "").strip()

    from ..models.database import get_config, set_config, list_configs, delete_config

    # set and delete require admin
    if action in ("set", "delete"):
        err = await _require_admin(caller_user_id)
        if err:
            return err

    if action == "set":
        if not key:
            return "⚠️ key é obrigatório para set."
        value = (inp.get("value") or "").strip()
        if not value:
            return "⚠️ value é obrigatório para set."
        await set_config(key, value, created_by=caller_user_id)
        return f"✅ Config `{key}` salva com sucesso."

    elif action == "get":
        if not key:
            return "⚠️ key é obrigatório para get."
        val = await get_config(key)
        return f"`{key}` = `{val}`" if val is not None else f"Config `{key}` não encontrada."

    elif action == "list":
        rows = await list_configs()
        if not rows:
            return "Nenhuma configuração dinâmica armazenada."
        lines = ["*Configurações dinâmicas:*"]
        for r in rows:
            updated = (r.get("updated_at") or "")[:10]
            by = r.get("created_by") or "?"
            # Truncate long values for display
            val_display = str(r["value"])[:60] + ("…" if len(str(r["value"])) > 60 else "")
            lines.append(f"• `{r['key']}` = `{val_display}` _(por {by}, {updated})_")
        return "\n".join(lines)

    elif action == "delete":
        if not key:
            return "⚠️ key é obrigatório para delete."
        deleted = await delete_config(key)
        return f"✅ Config `{key}` removida." if deleted else f"Config `{key}` não encontrada."

    return f"Ação desconhecida: {action}"
