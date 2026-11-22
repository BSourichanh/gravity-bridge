# Plugin Gravity Bridge

Plugin Antigravity autonome (**100 % bibliothèque standard Python 3**, zéro hook permanent) assurant la communication inter-workspaces via une boîte aux lettres SQLite centralisée (`~/.gemini/gravity-bridge/bridge.db`) et l'instanciation asynchrone de sessions `agy` en mode `stream-json`.

## Commandes `/gravity-bridge`

- `/gravity-bridge` *(défaut)* : enregistre le workspace actif si absent et affiche les messages ouverts/non lus et les jobs actifs ou récents.
- `/gravity-bridge workspaces` : liste les workspaces enregistrés ou enregistre un nouveau dossier local.
- `/gravity-bridge send <cible> [question|handoff|info] <objet>` : compose un brief autonome avec `file_refs` relatifs ; demande un feu vert humain unique avant `dispatch=true`.
- `/gravity-bridge read <thread_id>` : affiche le fil complet, les messages, le statut du dernier job et la commande `agy --conversation <id>`.
- `/gravity-bridge take <thread_id>` : prend en charge un `handoff` depuis le workspace cible et répond avec `--close`.
- `/gravity-bridge reply <thread_id>` : répond au fil (`--close` ou `--dispatch` pour relancer la conversation cible).
- `/gravity-bridge status [job_id]` : suit l'avancement des jobs `agy` ou annule un job actif (`cancel_job`).
- `/gravity-bridge done <thread_id>` · `prune` : clôture un fil ou purge manuellement les vieux fils `done` et leurs logs.

## Outils MCP (`gravity-bridge_bridge`)

`register_workspace`, `list_workspaces`, `send_message`, `list_inbox`, `read_thread`, `reply_thread`, `dispatch_thread`, `manage_thread`.

## Sécurité & isolation

- **Feu vert unique :** aucune session `accept-edits` ou instanciation `dispatch=true` n'est déclenchée sans validation humaine préalable.
- **Zéro pollution des dépôts :** aucune base ni aucun fichier temporaire n'est déposé dans les workspaces cibles.
- **Anti-boucle fail-closed :** `GRAVITY_BRIDGE_DEPTH >= 1` bloque tout sous-dispatch en cascade.
- **Filtre secrets & plafonds :** refus en entrée utilisateur (et caviardage en sortie worker/log) sur les clés AWS, Google, GitHub, `sk-...`, PEM et `Authorization: Bearer ...`, avec plafond à 32 Ko UTF-8 par message.
