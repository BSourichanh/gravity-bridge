---
name: gravity-bridge
description: >-
  Orchestre la boîte aux lettres SQLite inter-workspaces et l'instanciation asynchrone de sessions
  agy stream-json via le serveur MCP gravity-bridge_bridge. Activer uniquement sur
  /gravity-bridge [workspaces|send|read|take|reply|status|done|prune].
---

# Gravity Bridge — Communication Inter-Workspaces

## Règles strictes

1. **Résolution explicite du workspace :** déduire le workspace actif depuis `Active Workspaces` dans votre contexte et passer explicitement son slug ou son chemin absolu (`from_workspace`, `workspace`, `reader_workspace`). Ne jamais supposer un workspace par défaut en contexte multi-racines.
2. **Feu vert humain unique :** avant tout `send_message(..., dispatch=true)`, `reply_thread(..., dispatch=true)` ou `dispatch_thread`, présenter à l'utilisateur le workspace cible, le `msg_type`, le mode effectif (`plan` pour `question`/`info`, `accept-edits` pour `handoff`), l'objet et les `file_refs`, puis attendre sa confirmation explicite avant de passer `approved=true, dispatch=true`.
3. **Isolation & plafonds :** ne jamais coller de gros fichiers ni de secrets dans `body` (plafond 32 Ko UTF-8) ; référencer les fichiers concernés via `file_refs` relatifs au dépôt cible (max 20 entrées).
4. **Anti-boucle :** lorsque `GRAVITY_BRIDGE_DEPTH >= 1` (session instanciée par Gravity Bridge), tout sous-dispatch est bloqué ; répondre passivement via `reply_thread(..., dispatch=false)`.
5. **Réponses synthétiques :** limiter les résumés par défaut à **≤ 12 lignes** en Markdown clair (tableau ou puces).

## Routage des commandes

### 1. `/gravity-bridge` *(défaut)*
1. Appeler `register_workspace(path="<workspace_actif>")` s'il n'est pas encore enregistré.
2. Appeler `list_inbox(workspace="<slug>", direction="inbox", status="open")` et `manage_thread(action="get_job")` si des jobs existent.
3. Afficher un résumé compact (≤ 12 lignes) des messages `unread`/`open` et des jobs actifs/récents.

### 2. `/gravity-bridge workspaces`
- Appeler `list_workspaces()`. Si un chemin est fourni, appeler `register_workspace(path=...)` au préalable.

### 3. `/gravity-bridge send <cible> [question|handoff|info] <objet>`
1. Rédiger un brief autonome concis avec `file_refs` relatifs (sans dupliquer le contenu complet des fichiers).
2. Si un lancement automatique (`dispatch=true`) est demandé, demander la validation humaine unique avant d'appeler `send_message(..., approved=true, dispatch=true)`.
3. Sinon, déposer passivement dans la boîte aux lettres avec `send_message(..., dispatch=false)`.

### 4. `/gravity-bridge read <thread_id>`
- Appeler `read_thread(thread_id=..., reader_workspace="<slug>", mark_read=True)`.
- Afficher les métadonnées, les messages chronologiques, le statut du dernier job et, s'il est présent, la commande `resume_command` (`cd "..." && agy --conversation <id>`).

### 5. `/gravity-bridge take <thread_id>` *(depuis le workspace cible)*
1. Appeler `read_thread(thread_id=..., reader_workspace="<slug>", mark_read=True)`.
2. Si `msg_type == "handoff"` et `approved == true`, exécuter le lot dans le périmètre et sous les règles du dépôt courant (sans redemander de confirmation manuelle). Sinon, présenter le contenu et demander confirmation avant d'agir.
3. Clôturer en envoyant le rapport concis via `reply_thread(thread_id=..., from_workspace="<slug>", body=..., close=True)`.

### 6. `/gravity-bridge reply <thread_id>`
- Appeler `reply_thread(thread_id=..., from_workspace="<slug>", body=..., close=<--close>, dispatch=<--dispatch>)`.
- Si `--dispatch` est demandé, obtenir confirmation unique au préalable ; en v0.1, le re-dispatch est réservé au sens initial `from_workspace -> to_workspace` et reprend `target_conversation_id`.

### 7. `/gravity-bridge status [job_id]`
- Appeler `manage_thread(action="get_job", job_id=...)` (ou `cancel_job` si annulation demandée) et résumer l'avancement.

### 8. `/gravity-bridge done <thread_id>` · `prune`
- `done <thread_id>` : appeler `manage_thread(action="close", thread_id=...)`.
- `prune [days]` : appeler `manage_thread(action="prune_done", older_than_days=...)`.
