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
2. **Feu vert humain unique :** avant `dispatch=true` (`send_message`, `reply_thread`, `dispatch_thread`), sauf dispatch autonome `/gravity-architect` post-Planification, présenter cible, `msg_type`, `workflow`, objet et `file_refs`, puis attendre confirmation avant `approved=true, dispatch=true`. Passer `caller_conversation_id` si connu (`notify_caller` *Auto si prêt* si omis, sinon warning si `true`).
3. **Profil `workflow` (`send_message`, `dispatch_thread`) :** `none|quick|standard|deep|evo` (défaut `standard` sur `handoff`, `none` seul sur `question`/`info`). Sur `handoff` actif, le relais injecte `/gravity-workflow <profil>`.
4. **Fail-fast sidecar :** si cible `unlinked`/`offline`, proposer dépôt passif (`dispatch=false`) ou `/gravity-bridge link <slug>`.
5. **Isolation & plafonds :** stockage `$GRAVITY_BRIDGE_HOME` > `$GRAVITY_HOME/bridge` > `~/.gemini/gravity/bridge/bridge.db` ; 0 secret, `body` <= 32 Ko UTF-8, `file_refs` relatifs (`1..20` sur `reply_thread` de retour/clôture, jamais > 20).
6. **Anti-boucle, lots & TTL :** sous `depth >= 1`, 0 sous-dispatch ; répondre via `reply_thread(..., dispatch=false, conversation_id=..., file_refs=[...])` (`1..20`). Lots (`batch_id`, `callback_mode=per_thread|on_batch_complete`) : `on_batch_complete` agrège le callback final (`batch_summary`), bascule en fail-fast si `close=false`, et sérialise les jobs `handoff` outbound par cible (TTL `dispatched` 1800s via `GRAVITY_BRIDGE_DISPATCHED_STALE_SECONDS`).
7. **Synthèse :** résumés **<= 12 lignes** Markdown.

## Outils MCP (`gravity-bridge_bridge`) & Routage

| Commande | Outils MCP | Action |
| :--- | :--- | :--- |
| `(défaut)` | `register_workspace`, `list_workspaces`, `list_inbox`, `manage_thread` | Enregistre workspace actif et résume sidecars, messages `open` et jobs |
| `workspaces` | `list_workspaces`, `register_workspace` | Liste workspaces et statuts sidecar (`online`/`offline`/`unlinked`) |
| `link <slug> [path]` | `register_workspace`, `link_workspace` | Crée `sidecar.json` + `<nom> (Bridge)` et active `config.json` (`uninstall=true` pour délier) |
| `send <cible> ...` | `send_message`, `dispatch_thread` | Crée un fil (`question`\|`handoff`\|`info`, `batch_id`/`callback_mode`), ouvre conv si `dispatch=true` |
| `read <id>` | `read_thread` | Lit le fil (`mark_read=True`), messages, `target_conversation_id`, `batch_summary` et dernier job |
| `take <id>` | `read_thread`, `reply_thread` | Si `handoff` et `approved=true`, exécute sous `AGENTS.md` local puis clôt (`close=True, dispatch=False`) |
| `reply <id>` | `reply_thread` | Répond (`--close`, ou `--dispatch` sens `from -> to` avec `approved=true`) |
| `status [job]` | `manage_thread` | `get_job` ou `cancel_job` (`job_id` ou `thread_id`) |
| `done` / `prune` | `manage_thread` | `close` un fil ou `prune_done` (`older_than_days`) |

Exemples JSON et scénarios détaillés : [references/examples.md](references/examples.md).
