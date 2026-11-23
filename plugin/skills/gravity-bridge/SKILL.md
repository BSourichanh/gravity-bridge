---
name: gravity-bridge
description: >-
  Orchestre la boîte aux lettres SQLite inter-workspaces et l'ouverture de conversations natives
  dans Antigravity App via micro-sidecars agentapi (serveur MCP gravity-bridge_bridge). Activer
  uniquement sur /gravity-bridge [workspaces|link|send|read|take|reply|status|done|prune].
---

# Gravity Bridge

## Règles strictes

1. **Workspace explicite :** déduire le workspace actif depuis `Active Workspaces` et passer son slug ou chemin absolu (`from_workspace`, `workspace`, `reader_workspace`).
2. **Feu vert humain unique :** avant `send_message(..., dispatch=true)`, `reply_thread(..., dispatch=true)` ou `dispatch_thread`, présenter cible(s), `msg_type`, posture (`question`/`info` lecture seule, `handoff` exécution encadrée), objet et `file_refs`, puis attendre confirmation avant `approved=true, dispatch=true`. Passer `caller_conversation_id` si connu ; ce feu vert couvre envoi et callbacks (`notify_caller`, *Auto si prêt* si omis avec sidecar appelant `online`, sinon avertissement si `notify_caller=true`).
3. **Fail-fast sidecar :** si la cible est `unlinked`/`offline`, expliquer la cause et proposer dépôt passif (`dispatch=false`) ou `/gravity-bridge link <slug>`.
4. **Isolation & plafonds :** stockage centralisé (`$GRAVITY_BRIDGE_HOME` > `$GRAVITY_HOME/bridge` > `~/.gemini/gravity/bridge/bridge.db`) ; zéro secret ni gros fichier dans `body` (<= 32 Ko UTF-8) ; `file_refs` relatifs (<= 20).
5. **Anti-boucle & callbacks :** sous `depth >= 1` (`GRAVITY_BRIDGE_DEPTH >= 1`), aucun sous-dispatch ; répondre via `reply_thread(..., dispatch=false, conversation_id=...)`. Si une réponse cible `close=false` clôt un job outbound actif, le serveur enfile un callback intermédiaire `status=open` (sans `callback_dispatched_at`) ; à la clôture (`close=true`), il déclenche le callback final *one-shot* `status=done` si sidecar appelant `online` (sinon `skipped_sidecar_*`).
6. **Synthèse :** résumés par défaut **<= 12 lignes** Markdown.

## Outils MCP (`gravity-bridge_bridge`) & Routage

| Commande | Outils MCP | Action |
| :--- | :--- | :--- |
| `(défaut)` | `register_workspace`, `list_workspaces`, `list_inbox`, `manage_thread` | Enregistre workspace actif et résume sidecars, messages `open` et jobs |
| `workspaces` | `list_workspaces`, `register_workspace` | Liste workspaces et statuts sidecar (`online`/`offline`/`unlinked`) |
| `link <slug> [path]` | `register_workspace`, `link_workspace` | Crée `sidecar.json` + `<nom> (Bridge)` et active `config.json` (`uninstall=true` pour délier) |
| `send <cible> ...` | `send_message`, `dispatch_thread` | Crée un fil (`question`\|`handoff`\|`info`), ouvre la conv si `dispatch=true` (ou dépôt passif) |
| `read <id>` | `read_thread` | Lit le fil (`mark_read=True`), messages, `target_conversation_id` et dernier job |
| `take <id>` | `read_thread`, `reply_thread` | Si `handoff` et `approved=true`, exécute sous `AGENTS.md` local puis clôt (`close=True, dispatch=False`) |
| `reply <id>` | `reply_thread` | Répond (`--close`, ou `--dispatch` sens `from -> to` avec `approved=true`) |
| `status [job]` | `manage_thread` | `get_job` ou `cancel_job` (`queued`/`claimed`/`dispatched`/`completed`/`failed`/`cancelled`) |
| `done` / `prune` | `manage_thread` | `close` un fil ou `prune_done` (`older_than_days`) |

Exemples JSON et scénarios détaillés : [references/examples.md](references/examples.md).
