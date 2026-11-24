#!/usr/bin/env python3
"""Serveur MCP stdio JSON-RPC 2.0 pour Gravity Bridge (100 % stdlib Python 3)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Optional

MCP_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = MCP_DIR.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from core.security import (  # noqa: E402
    NotFoundError,
    ValidationError,
    assert_dispatch_allowed,
    assert_dispatch_approved,
    validate_body,
    validate_conversation_id,
    validate_file_refs,
    validate_int_range,
    validate_model,
    validate_msg_type,
    validate_wait_seconds,
    validate_workflow_profile,
)
from core.sidecar_manager import link_workspace_sidecar  # noqa: E402
from core.store import BridgeStore, resolve_notify_caller  # noqa: E402

SERVER_NAME = "gravity-bridge"
SERVER_VERSION = "0.3.1"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


def _tool_ok(structured: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, ensure_ascii=False, indent=2),
            }
        ],
        "structuredContent": structured,
        "isError": False,
    }


def _tool_err(message: str) -> Dict[str, Any]:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


def _as_bool(val: Any, name: str, default: bool) -> bool:
    if val is None:
        return default
    if not isinstance(val, bool):
        raise ValidationError(f"Le paramètre '{name}' doit être un booléen.")
    return val


def _optional_bool(args: Dict[str, Any], key: str) -> Optional[bool]:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, bool):
        raise ValidationError(f"Le paramètre '{key}' doit être un booléen.")
    return val


def _require_str(args: Dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValidationError(f"Le paramètre '{key}' est requis.")
    return val


def _optional_str(args: Dict[str, Any], key: str) -> Optional[str]:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise ValidationError(f"Le paramètre '{key}' doit être une chaîne de caractères.")
    stripped = val.strip()
    return stripped or None


def _tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "register_workspace",
            "title": "Enregistrer un workspace Gravity Bridge",
            "description": (
                "Enregistre ou met à jour un workspace local par son chemin de répertoire "
                "dans la boîte aux lettres centralisée Gravity Bridge."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Chemin local du répertoire du workspace.",
                    },
                    "slug": {
                        "type": "string",
                        "description": "Slug personnalisé optionnel [a-z0-9][a-z0-9_-]{0,48}.",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Nom d'affichage lisible optionnel.",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Identifiant de projet Antigravity App optionnel.",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "link_workspace",
            "title": "Lier ou délier le micro-sidecar d'un workspace",
            "description": (
                "Génère (ou supprime si uninstall=true) le micro-sidecar ~/.gemini/config/sidecars/"
                "gravity-bridge-<slug>/sidecar.json, crée/met à jour son projet sidecar dédié '<nom> (Bridge)' "
                "dans ~/.gemini/config/projects/*.json (miroir des dossiers/réglages/permissions du projet utilisateur "
                "sans jamais attacher le projet utilisateur au sidecar), active config.json (schemaVersion=1) et "
                "répare tout projet utilisateur précédemment pollué par 'sidecar'."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": {
                        "type": "string",
                        "description": "Slug existant ou chemin explicite du workspace à lier.",
                    },
                    "uninstall": {
                        "type": "boolean",
                        "description": "Supprime le micro-sidecar et son projet dédié '<nom> (Bridge)' si true (défaut false).",
                    },
                    "project_id": {
                        "type": "string",
                        "description": (
                            "Identifiant explicite du projet sidecar dédié '<nom> (Bridge)' (optionnel ; généré ou "
                            "réutilisé automatiquement si omis). Refusé s'il désigne un projet utilisateur existant."
                        ),
                    },
                    "source_project_id": {
                        "type": "string",
                        "description": (
                            "Identifiant optionnel du projet utilisateur Antigravity App dont recopier "
                            "projectResources, settings et permissionGrants dans le projet dédié '<nom> (Bridge)' "
                            "(détecté automatiquement par défaut)."
                        ),
                    },
                    "configure_config": {
                        "type": "boolean",
                        "description": (
                            "Met à jour projects/*.json et ~/.gemini/config/config.json (défaut/omission = true, "
                            "false = écrit uniquement sidecar.json sans toucher ni à config.json ni à projects/)."
                        ),
                    },
                },
                "required": ["workspace"],
            },
        },
        {
            "name": "list_workspaces",
            "title": "Lister les workspaces enregistrés",
            "description": (
                "Liste les workspaces enregistrés avec leur statut sidecar (online/offline/unlinked), "
                "open_inbox_count, unread_count et active_jobs_count."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "send_message",
            "title": "Envoyer un fil inter-workspaces",
            "description": (
                "Enregistre un nouveau fil typé (question, handoff, info) et peut créer une conversation "
                "native dans le projet Antigravity App cible via son micro-sidecar si dispatch=true."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from_workspace": {
                        "type": "string",
                        "description": "Slug existant ou chemin explicite du workspace émetteur.",
                    },
                    "to_workspace": {
                        "type": "string",
                        "description": "Slug existant ou chemin explicite du workspace cible.",
                    },
                    "msg_type": {
                        "type": "string",
                        "enum": ["question", "handoff", "info"],
                        "description": "Type du fil (question, handoff ou info).",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Objet court du fil (<= 160 caractères).",
                    },
                    "body": {
                        "type": "string",
                        "description": "Corps du message (<= 32 Ko UTF-8, sans secrets).",
                    },
                    "file_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Chemins relatifs informatifs de fichiers (max 20).",
                    },
                    "approved": {
                        "type": "boolean",
                        "description": "Acte le feu vert humain unique (requis si dispatch=true).",
                    },
                    "dispatch": {
                        "type": "boolean",
                        "description": "Crée la conversation distante via le micro-sidecar cible si true.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Sélecteur de modèle agentapi optionnel (flash_lite, flash, pro ou model_id).",
                    },
                    "workflow": {
                        "type": "string",
                        "enum": ["none", "quick", "standard", "deep", "evo"],
                        "description": (
                            "Profil /gravity-workflow optionnel (défaut 'standard' sur handoff, "
                            "'none' seul autorisé sur question/info)."
                        ),
                    },
                    "caller_conversation_id": {
                        "type": "string",
                        "description": (
                            "Conversation ID appelant optionnel (anti-boucle et cible du callback retour one-shot)."
                        ),
                    },
                    "notify_caller": {
                        "type": "boolean",
                        "description": (
                            "Active ou désactive le callback retour one-shot vers caller_conversation_id à la clôture "
                            "(omis = auto si caller_conversation_id fourni et sidecar appelant online)."
                        ),
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "description": "Attente synchrone bornée en secondes (0..30, défaut 0).",
                    },
                },
                "required": ["from_workspace", "to_workspace", "msg_type", "subject", "body"],
            },
        },
        {
            "name": "list_inbox",
            "title": "Consulter la boîte aux lettres",
            "description": (
                "Retourne de façon non destructive le résumé compact des fils d'un workspace "
                "triés par updated_at décroissant."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workspace": {
                        "type": "string",
                        "description": "Slug ou chemin explicite enregistré du workspace.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["inbox", "outbox", "all"],
                        "description": "Filtre directionnel (défaut inbox).",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["open", "done", "all"],
                        "description": "Filtre de statut (défaut open).",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "Uniquement les fils contenant des messages non lus.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Nombre maximal de fils retournés (1..200, défaut 20).",
                    },
                },
                "required": ["workspace"],
            },
        },
        {
            "name": "read_thread",
            "title": "Lire un fil complet",
            "description": (
                "Retourne les métadonnées, messages chronologiques, file_refs, "
                "target_conversation_id, target_sidecar_status et dernier job d'un fil."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Identifiant du fil (thr-...).",
                    },
                    "reader_workspace": {
                        "type": "string",
                        "description": "Workspace lecteur pour marquer les messages adverses lus.",
                    },
                    "mark_read": {
                        "type": "boolean",
                        "description": "Marque les messages adverses comme lus si true (défaut true).",
                    },
                },
                "required": ["thread_id"],
            },
        },
        {
            "name": "reply_thread",
            "title": "Répondre à un fil",
            "description": (
                "Ajoute une réponse à un fil existant, marque les messages adverses lus, "
                "clôt automatiquement tout job dispatched si émis par la cible, déclenche l'éventuel callback "
                "retour (final si close=true, intermédiaire si close=false après un job outbound actif) "
                "et peut relancer la conversation App si dispatch=true."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Identifiant du fil (thr-...).",
                    },
                    "from_workspace": {
                        "type": "string",
                        "description": "Slug ou chemin explicite du workspace qui répond.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Corps de la réponse (<= 32 Ko UTF-8, sans secrets).",
                    },
                    "file_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Chemins relatifs informatifs de fichiers (max 20).",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "Conversation ID de la session cible (filet de sécurité sur réponse retour).",
                    },
                    "close": {
                        "type": "boolean",
                        "description": "Clôture le fil à done si true (défaut false).",
                    },
                    "dispatch": {
                        "type": "boolean",
                        "description": "Relance la conversation cible via agentapi send-message si true (sens from -> to).",
                    },
                    "approved": {
                        "type": "boolean",
                        "description": "Feu vert humain unique requis si dispatch=true.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Sélecteur de modèle agentapi optionnel (en cas de repli new-conversation).",
                    },
                    "caller_conversation_id": {
                        "type": "string",
                        "description": "Conversation ID appelant optionnel pour le garde-fou anti-boucle.",
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "description": "Attente synchrone bornée en secondes (0..30, défaut 0).",
                    },
                },
                "required": ["thread_id", "from_workspace", "body"],
            },
        },
        {
            "name": "dispatch_thread",
            "title": "Créer ou relancer la conversation App d'un fil",
            "description": (
                "Déclenche ou poursuit explicitement une conversation Antigravity App sur un fil "
                "existant via le micro-sidecar du projet cible (approved=true et sidecar online requis)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": "Identifiant du fil (thr-...).",
                    },
                    "approved": {
                        "type": "boolean",
                        "description": "Acte le feu vert humain unique requis avant dispatch.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Sélecteur de modèle agentapi optionnel.",
                    },
                    "workflow": {
                        "type": "string",
                        "enum": ["none", "quick", "standard", "deep", "evo"],
                        "description": (
                            "Profil /gravity-workflow optionnel pour mettre à jour le fil avant dispatch "
                            "(omis = préserve l'existant)."
                        ),
                    },
                    "caller_conversation_id": {
                        "type": "string",
                        "description": (
                            "Conversation ID appelant optionnel (anti-boucle et cible du callback retour one-shot)."
                        ),
                    },
                    "notify_caller": {
                        "type": "boolean",
                        "description": (
                            "Active ou désactive le callback retour one-shot vers caller_conversation_id à la clôture "
                            "(omis = préserve l'existant ou auto si prêt)."
                        ),
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "description": "Attente synchrone bornée en secondes (0..30, défaut 0).",
                    },
                },
                "required": ["thread_id"],
            },
        },
        {
            "name": "manage_thread",
            "title": "Gérer l'état d'un fil ou d'un job",
            "description": (
                "Permet de clôturer/rouvrir un fil, consulter l'état des jobs, "
                "annuler un job ou purger manuellement les vieux fils done."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["close", "reopen", "get_job", "cancel_job", "prune_done"],
                        "description": "Action de gestion à exécuter.",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Identifiant du fil (requis pour close/reopen, optionnel pour get_job).",
                    },
                    "job_id": {
                        "type": "string",
                        "description": "Identifiant du job (requis pour cancel_job, optionnel pour get_job).",
                    },
                    "older_than_days": {
                        "type": "integer",
                        "description": "Âge minimal en jours pour prune_done (défaut 30).",
                    },
                },
                "required": ["action"],
            },
        },
    ]


def _handle_register_workspace(args: Dict[str, Any]) -> Dict[str, Any]:
    path = _require_str(args, "path")
    slug = _optional_str(args, "slug")
    display_name = _optional_str(args, "display_name")
    project_id = _optional_str(args, "project_id")
    store = BridgeStore()
    return _tool_ok(
        store.register_workspace(
            path, slug=slug, display_name=display_name, project_id=project_id
        )
    )


def _handle_link_workspace(args: Dict[str, Any]) -> Dict[str, Any]:
    workspace = _require_str(args, "workspace")
    uninstall = _as_bool(args.get("uninstall"), "uninstall", False)
    project_id = _optional_str(args, "project_id")
    source_project_id = _optional_str(args, "source_project_id")
    configure_config = _optional_bool(args, "configure_config")
    store = BridgeStore()
    return _tool_ok(
        link_workspace_sidecar(
            store,
            workspace,
            project_id=project_id,
            source_project_id=source_project_id,
            configure_config=configure_config,
            uninstall=uninstall,
        )
    )


def _handle_list_workspaces(_args: Dict[str, Any]) -> Dict[str, Any]:
    store = BridgeStore()
    workspaces = store.list_workspaces(reconcile=True)
    return _tool_ok({"count": len(workspaces), "workspaces": workspaces})


def _handle_send_message(args: Dict[str, Any]) -> Dict[str, Any]:
    from_workspace = _require_str(args, "from_workspace")
    to_workspace = _require_str(args, "to_workspace")
    msg_type = validate_msg_type(_require_str(args, "msg_type").strip(), allow_reply=False)
    subject = _require_str(args, "subject")
    body = _require_str(args, "body")
    file_refs = args.get("file_refs")
    approved = _as_bool(args.get("approved"), "approved", False)
    dispatch = _as_bool(args.get("dispatch"), "dispatch", False)
    model = validate_model(_optional_str(args, "model"))
    workflow = _optional_str(args, "workflow")
    validate_workflow_profile(workflow, msg_type=msg_type)
    caller_conv_id = validate_conversation_id(
        _optional_str(args, "caller_conversation_id"),
        field_name="caller_conversation_id",
    )
    notify_caller = _optional_bool(args, "notify_caller")
    wait_seconds = validate_wait_seconds(args.get("wait_seconds"))

    store = BridgeStore()
    if dispatch:
        assert_dispatch_approved(approved)
        assert_dispatch_allowed(
            caller_conversation_id=caller_conv_id,
            known_target_conversation_ids=store.list_target_conversation_ids(),
        )
        recipient = store.resolve_workspace(to_workspace, allow_auto_register=True)
        store.assert_sidecar_online(recipient["slug"])

    created = store.create_thread(
        from_workspace=from_workspace,
        to_workspace=to_workspace,
        msg_type=msg_type,
        subject=subject,
        body=body,
        file_refs=file_refs,
        approved=approved,
        caller_conversation_id=caller_conv_id,
        notify_caller=notify_caller,
        workflow=workflow,
    )
    if dispatch:
        job = store.create_job(
            thread_id=created["thread_id"],
            message_id=created["message_id"],
            target_workspace=created["to_workspace"],
            action="new_conversation",
            model=model,
            approve_thread=approved,
            require_sidecar_online=True,
        )
        if wait_seconds > 0:
            job = store.wait_for_job_dispatch(job["id"], wait_seconds=wait_seconds)
        created["job"] = job
    return _tool_ok(created)


def _handle_list_inbox(args: Dict[str, Any]) -> Dict[str, Any]:
    workspace = _require_str(args, "workspace")
    direction = _optional_str(args, "direction") or "inbox"
    status = _optional_str(args, "status") or "open"
    unread_only = _as_bool(args.get("unread_only"), "unread_only", False)
    limit = validate_int_range(
        args.get("limit"),
        name="limit",
        default=20,
        min_value=1,
        max_value=200,
    )
    store = BridgeStore()
    ws = store.get_workspace(workspace)
    threads = store.list_inbox(
        ws["slug"],
        direction=direction,
        status=status,
        unread_only=unread_only,
        limit=limit,
        reconcile=True,
    )
    return _tool_ok(
        {
            "workspace": ws["slug"],
            "direction": direction,
            "status": status,
            "count": len(threads),
            "threads": threads,
        }
    )


def _handle_read_thread(args: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = _require_str(args, "thread_id").strip()
    reader_workspace = _optional_str(args, "reader_workspace")
    mark_read = _as_bool(args.get("mark_read"), "mark_read", True)
    store = BridgeStore()
    detail = store.read_thread(
        thread_id,
        reader_workspace=reader_workspace,
        mark_read=mark_read,
        reconcile=True,
    )
    return _tool_ok(detail)


def _handle_reply_thread(args: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = _require_str(args, "thread_id").strip()
    from_workspace = _require_str(args, "from_workspace")
    body = validate_body(_require_str(args, "body"))
    file_refs = validate_file_refs(args.get("file_refs"))
    conversation_id = validate_conversation_id(
        _optional_str(args, "conversation_id"), field_name="conversation_id"
    )
    close = _as_bool(args.get("close"), "close", False)
    dispatch = _as_bool(args.get("dispatch"), "dispatch", False)
    approved = _as_bool(args.get("approved"), "approved", False)
    model = validate_model(_optional_str(args, "model"))
    caller_conv_id = validate_conversation_id(
        _optional_str(args, "caller_conversation_id"),
        field_name="caller_conversation_id",
    )
    wait_seconds = validate_wait_seconds(args.get("wait_seconds"))

    store = BridgeStore()
    target_slug: Optional[str] = None
    if dispatch:
        if close:
            raise ValidationError(
                "Impossible de combiner close=true et dispatch=true dans reply_thread."
            )
        assert_dispatch_approved(approved)
        assert_dispatch_allowed(
            caller_conversation_id=caller_conv_id,
            known_target_conversation_ids=store.list_target_conversation_ids(),
        )
        thread = store.read_thread(thread_id, mark_read=False, reconcile=True)
        sender = store.resolve_workspace(from_workspace, allow_auto_register=True)
        if sender["slug"] != thread["from_workspace"]:
            raise ValidationError(
                "reply_thread(..., dispatch=true) est réservé à l'émetteur initial "
                f"({thread['from_workspace']!r} -> {thread['to_workspace']!r})."
            )
        pending = [
            j
            for j in store.list_jobs(thread_id=thread_id, reconcile=False)
            if j["status"] in ("queued", "claimed")
        ]
        if pending:
            raise ValidationError(
                f"Un job en attente existe déjà sur le fil {thread_id} ({pending[0]['id']})."
            )
        target_slug = thread["to_workspace"]
        store.assert_sidecar_online(target_slug)

    replied = store.add_reply(
        thread_id=thread_id,
        from_workspace=from_workspace,
        body=body,
        file_refs=file_refs,
        close=close,
        conversation_id=conversation_id,
    )
    if dispatch and target_slug is not None:
        job = store.create_job(
            thread_id=thread_id,
            message_id=replied["message_id"],
            target_workspace=target_slug,
            model=model,
            approve_thread=approved,
            require_sidecar_online=True,
        )
        if wait_seconds > 0:
            job = store.wait_for_job_dispatch(job["id"], wait_seconds=wait_seconds)
        replied["job"] = job
    elif wait_seconds > 0 and replied.get("callback_job"):
        cb_job = store.wait_for_job_dispatch(
            replied["callback_job"]["id"], wait_seconds=wait_seconds
        )
        replied["callback_job"] = cb_job
        if cb_job["status"] in ("dispatched", "completed"):
            replied["callback_status"] = "dispatched"
        elif cb_job["status"] == "failed":
            replied["callback_status"] = "failed"
        else:
            replied["callback_status"] = cb_job["status"]
    return _tool_ok(replied)


def _handle_dispatch_thread(args: Dict[str, Any]) -> Dict[str, Any]:
    thread_id = _require_str(args, "thread_id").strip()
    approved = _as_bool(args.get("approved"), "approved", False)
    model = validate_model(_optional_str(args, "model"))
    workflow = _optional_str(args, "workflow")
    caller_conv_id = validate_conversation_id(
        _optional_str(args, "caller_conversation_id"),
        field_name="caller_conversation_id",
    )
    notify_caller = _optional_bool(args, "notify_caller")
    wait_seconds = validate_wait_seconds(args.get("wait_seconds"))

    store = BridgeStore()
    assert_dispatch_approved(approved)
    assert_dispatch_allowed(
        caller_conversation_id=caller_conv_id,
        known_target_conversation_ids=store.list_target_conversation_ids(),
    )

    thread = store.read_thread(thread_id, mark_read=False, reconcile=True)
    messages = thread.get("messages") or []
    if not messages:
        raise NotFoundError(f"Aucun message à dispatcher dans le fil {thread_id!r}.")

    from_slug = thread["from_workspace"]
    sender_msgs = [m for m in messages if m.get("sender_workspace") == from_slug]
    target_msg = sender_msgs[-1] if sender_msgs else messages[-1]

    caller_ws = store.get_workspace(from_slug)
    effective_caller = (
        caller_conv_id if caller_conv_id is not None else thread.get("caller_conversation_id")
    )
    resolved_notify, warning = resolve_notify_caller(
        caller_ws,
        caller_conversation_id=effective_caller,
        notify_caller=notify_caller,
        existing_notify=bool(thread.get("notify_caller")),
    )

    job = store.create_job(
        thread_id=thread_id,
        message_id=int(target_msg["id"]),
        target_workspace=thread["to_workspace"],
        model=model,
        workflow=workflow,
        approve_thread=approved,
        require_sidecar_online=True,
        caller_conversation_id=caller_conv_id,
        notify_caller=notify_caller,
        sync_callback=True,
    )
    if wait_seconds > 0:
        job = store.wait_for_job_dispatch(job["id"], wait_seconds=wait_seconds)
    effective_workflow = (
        validate_workflow_profile(workflow, msg_type=thread["msg_type"])
        if workflow is not None
        else thread.get("workflow_profile", "none")
    )
    result: Dict[str, Any] = {
        "thread_id": thread_id,
        "message_id": int(target_msg["id"]),
        "caller_conversation_id": effective_caller,
        "notify_caller": resolved_notify,
        "workflow_profile": effective_workflow,
        "job": job,
    }
    if warning:
        result["warnings"] = [warning]
    return _tool_ok(result)


def _handle_manage_thread(args: Dict[str, Any]) -> Dict[str, Any]:
    action = _require_str(args, "action").strip()
    store = BridgeStore()

    if action == "close":
        return _tool_ok(store.close_thread(_require_str(args, "thread_id").strip()))
    if action == "reopen":
        return _tool_ok(store.reopen_thread(_require_str(args, "thread_id").strip()))
    if action == "get_job":
        job_id = _optional_str(args, "job_id")
        thread_id = _optional_str(args, "thread_id")
        if job_id:
            return _tool_ok(store.get_job(job_id, reconcile=True))
        jobs = store.list_jobs(thread_id=thread_id, reconcile=True)
        return _tool_ok({"count": len(jobs), "jobs": jobs})
    if action == "cancel_job":
        job_id = _require_str(args, "job_id").strip()
        return _tool_ok(store.cancel_job(job_id))
    if action == "prune_done":
        raw_days = args.get("older_than_days")
        days = validate_int_range(
            raw_days,
            name="older_than_days",
            default=30,
            min_value=0,
            max_value=3650,
        )
        return _tool_ok(store.prune_done(older_than_days=days))

    raise ValidationError(
        f"action invalide ({action!r}). Valeurs : close, reopen, get_job, cancel_job, prune_done."
    )


_TOOL_DISPATCH: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "register_workspace": _handle_register_workspace,
    "link_workspace": _handle_link_workspace,
    "list_workspaces": _handle_list_workspaces,
    "send_message": _handle_send_message,
    "list_inbox": _handle_list_inbox,
    "read_thread": _handle_read_thread,
    "reply_thread": _handle_reply_thread,
    "dispatch_thread": _handle_dispatch_thread,
    "manage_thread": _handle_manage_thread,
}


def handle_jsonrpc_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Traite un message JSON-RPC 2.0 MCP et retourne la réponse (None pour les notifications)."""
    if not isinstance(msg, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    req_id = msg.get("id")
    has_id = "id" in msg and req_id is not None
    method = msg.get("method")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}

    if not has_id:
        return None

    if not isinstance(method, str):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    try:
        if method == "initialize":
            client_proto = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            result: Dict[str, Any] = {
                "protocolVersion": client_proto,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_definitions()}
        elif method == "tools/call":
            tool_name = str(params.get("name") or "")
            tool_args = params.get("arguments")
            if not isinstance(tool_args, dict):
                tool_args = {}
            handler = _TOOL_DISPATCH.get(tool_name)
            if handler is None:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"},
                }
            try:
                result = handler(tool_args)
            except Exception as exc:
                result = _tool_err(str(exc))
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": str(exc)},
        }

    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _write_jsonrpc(payload: Dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    while True:
        raw_line = sys.stdin.buffer.readline()
        if not raw_line:
            break
        text = raw_line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            msg = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            _write_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
            )
            continue
        response = handle_jsonrpc_message(msg)
        if response is not None:
            _write_jsonrpc(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
