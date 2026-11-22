---
name: gravity-bridge
description: >-
  Orchestre la boîte aux lettres SQLite inter-workspaces et l'ouverture de conversations natives
  dans Antigravity App via micro-sidecars agentapi (serveur MCP gravity-bridge_bridge). Activer
  uniquement sur /gravity-bridge [workspaces|link|send|read|take|reply|status|done|prune].
---

# Gravity Bridge — Communication Inter-Projets Antigravity App

## Règles strictes

1. **Résolution explicite du workspace :** déduire le workspace actif depuis `Active Workspaces` dans votre contexte et passer explicitement son slug ou son chemin absolu (`from_workspace`, `workspace`, `reader_workspace`). Ne jamais supposer un workspace par défaut en contexte multi-racines.
2. **Feu vert humain unique :** avant tout `send_message(..., dispatch=true)`, `reply_thread(..., dispatch=true)` ou `dispatch_thread`, présenter à l'utilisateur le ou les workspaces cibles, le `msg_type`, la posture attendue (lecture seule pour `question`/`info`, exécution encadrée pour `handoff`), l'objet et les `file_refs`, puis attendre sa confirmation explicite (une validation groupée suffit en multi-cibles) avant de passer `approved=true, dispatch=true`. Passer `caller_conversation_id` s'il est connu dans le contexte.
3. **Fail-fast sidecar :** si un dispatch échoue parce que le micro-sidecar cible est `unlinked` ou `offline`, expliquer immédiatement la cause et proposer soit un dépôt passif (`dispatch=false`), soit `/gravity-bridge link <slug>` et l'activation dans Antigravity App.
4. **Isolation & plafonds :** stockage centralisé exclusivement hors des dépôts cibles (`$GRAVITY_BRIDGE_HOME` > `$GRAVITY_HOME/bridge` > `~/.gemini/gravity/bridge/bridge.db`) ; ne jamais coller de gros fichiers ni de secrets dans `body` (plafond 32 Ko UTF-8) ; référencer les fichiers concernés via `file_refs` relatifs au dépôt cible (max 20 entrées).
5. **Anti-boucle :** dans toute conversation ouverte par Gravity Bridge (`depth=1` dans le prompt ou `GRAVITY_BRIDGE_DEPTH >= 1`), tout sous-dispatch est strictement interdit ; répondre uniquement via `reply_thread(..., dispatch=false, conversation_id=...)`.
6. **Réponses synthétiques :** limiter les résumés par défaut à **≤ 12 lignes** en Markdown clair (tableau ou puces).

## Routage des commandes

### 1. `/gravity-bridge` *(défaut)*
1. Appeler `register_workspace(path="<workspace_actif>")` s'il n'est pas encore enregistré, puis `list_workspaces()` pour l'état des sidecars (`online`/`offline`/`unlinked`).
2. Appeler `list_inbox(workspace="<slug>", direction="inbox", status="open")` et `manage_thread(action="get_job")`.
3. Afficher un résumé compact (≤ 12 lignes) des sidecars, messages `unread`/`open` et jobs actifs/récents.

### 2. `/gravity-bridge workspaces`
- Appeler `list_workspaces()`. Si un chemin est fourni, appeler `register_workspace(path=...)` au préalable.

### 3. `/gravity-bridge link <slug> [chemin]`
1. Si un chemin est fourni, appeler `register_workspace(path=..., slug=...)` au préalable.
2. Appeler `link_workspace(workspace="<slug>")` (ou `uninstall=true` si déliaison demandée). L'outil génère `sidecar.json`, crée ou met à jour un **projet sidecar dédié `<nom> (Bridge)`** dans `~/.gemini/config/projects/*.json` (en recopiant `projectResources`, `settings` et `permissionGrants` du projet utilisateur détecté, ou en rattachant le dossier local s'il n'existe pas encore), active le sidecar dans `~/.gemini/config/config.json` (`"schemaVersion": 1`) et répare tout projet utilisateur précédemment marqué `"sidecar"`. Ainsi, votre projet utilisateur reste toujours visible dans `Projects` même avec `0` conversation, tandis que `<nom> (Bridge)` s'affiche uniquement lorsqu'une conversation Bridge est ouverte.
3. Si des projets utilisateur ont été réparés, informer l'utilisateur de recharger/redémarrer Antigravity App ; sinon confirmer l'activation et vérifier `online` via `/gravity-bridge workspaces`.

### 4. `/gravity-bridge send <cible> [question|handoff|info] <objet>`
1. Rédiger un brief autonome concis avec `file_refs` relatifs (sans dupliquer le contenu complet des fichiers).
2. Si une création de conversation (`dispatch=true`) est demandée, obtenir le feu vert humain unique puis appeler `send_message(..., approved=true, dispatch=true, caller_conversation_id=...)`.
3. Sinon (ou en repli si sidecar hors ligne), déposer passivement dans la boîte aux lettres avec `send_message(..., dispatch=false)`.

### 5. `/gravity-bridge read <thread_id>`
- Appeler `read_thread(thread_id=..., reader_workspace="<slug>", mark_read=True)`.
- Afficher les métadonnées (`target_conversation_id`, `target_sidecar_status`), les messages chronologiques et le statut du dernier job.

### 6. `/gravity-bridge take <thread_id>` *(depuis le projet cible dans Antigravity App)*
1. Appeler `read_thread(thread_id=..., reader_workspace="<slug>", mark_read=True)`.
2. Si `msg_type == "handoff"` et `approved == true`, exécuter le lot dans le périmètre et sous les règles du dépôt courant (sans redemander de confirmation manuelle). Sinon, présenter le contenu et demander confirmation avant d'agir.
3. Clôturer en envoyant le rapport concis via `reply_thread(thread_id=..., from_workspace="<slug>", body=..., conversation_id=..., close=True, dispatch=False)`.

### 7. `/gravity-bridge reply <thread_id>`
- Appeler `reply_thread(thread_id=..., from_workspace="<slug>", body=..., conversation_id=..., close=<--close>, dispatch=<--dispatch>, approved=...)`.
- Si `--dispatch` est demandé, obtenir confirmation unique au préalable (`approved=true`) ; le re-dispatch est réservé au sens initial `from_workspace -> to_workspace` et poursuit `target_conversation_id` via `agentapi send-message`.

### 8. `/gravity-bridge status [job_id]`
- Appeler `manage_thread(action="get_job", job_id=...)` (ou `cancel_job` si annulation demandée) et résumer l'avancement (`queued`, `claimed`, `dispatched`, `completed`, `failed`, `cancelled`).

### 9. `/gravity-bridge done <thread_id>` · `prune`
- `done <thread_id>` : appeler `manage_thread(action="close", thread_id=...)`.
- `prune [days]` : appeler `manage_thread(action="prune_done", older_than_days=...)`.
