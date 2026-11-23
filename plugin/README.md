# Plugin Gravity Bridge

Plugin Antigravity autonome (**100 % bibliothèque standard Python 3**, zéro CLI `agy`, zéro hook permanent, contrat `config/module.json` `id: "bridge"`) assurant la communication inter-projets via une boîte aux lettres SQLite centralisée (`$GRAVITY_BRIDGE_HOME` > `$GRAVITY_HOME/bridge` > `~/.gemini/gravity/bridge/bridge.db`) et des micro-sidecars `agentapi` ouvrant de vraies conversations natives dans **Antigravity App**.

## Commandes `/gravity-bridge`

- `/gravity-bridge` *(défaut)* : enregistre le workspace actif si absent et affiche les sidecars (`online`/`offline`/`unlinked`), messages ouverts/non lus et jobs actifs ou récents.
- `/gravity-bridge workspaces` : liste les workspaces enregistrés et leur statut sidecar, ou enregistre un nouveau dossier local.
- `/gravity-bridge link <slug> [chemin]` : génère `~/.gemini/config/sidecars/gravity-bridge-<slug>/sidecar.json`, crée ou met à jour son projet sidecar dédié `<nom> (Bridge)` dans `~/.gemini/config/projects/*.json` (miroir des dossiers/réglages/permissions du projet utilisateur, qui reste intact et toujours visible même vide) et l'active dans `~/.gemini/config/config.json` (`"schemaVersion": 1`).
- `/gravity-bridge send <cible> [question|handoff|info] <objet>` : compose un brief autonome avec `file_refs` relatifs ; demande un feu vert humain unique avant `dispatch=true` (fail-fast si sidecar cible non `online`) et résout `notify_caller` (*Auto si prêt* ou explicite) pour le callback retour *one-shot*.
- `/gravity-bridge read <thread_id>` : affiche le fil complet, les messages, `target_conversation_id`, `target_sidecar_status` et le dernier job.
- `/gravity-bridge take <thread_id>` : prend en charge un `handoff` depuis le projet cible et clôture via `reply_thread(..., close=True, dispatch=False)` (ce qui déclenche côté serveur l'éventuel callback retour *one-shot* vers `caller_conversation_id`).
- `/gravity-bridge reply <thread_id>` : répond au fil (`--close` ou `--dispatch` pour poursuivre la conversation cible via `agentapi send-message`).
- `/gravity-bridge status [job_id]` : suit l'avancement des jobs (`queued`, `claimed`, `dispatched`, `completed`, `failed`, `cancelled`) ou annule un job (`cancel_job`).
- `/gravity-bridge done <thread_id>` · `prune` : clôture un fil ou purge manuellement les vieux fils `done` et leurs logs.

## Outils MCP (`gravity-bridge_bridge`)

`register_workspace`, `link_workspace`, `list_workspaces`, `send_message`, `list_inbox`, `read_thread`, `reply_thread`, `dispatch_thread`, `manage_thread`.

## Installation (`install.py`)

`install.py` suit le contrat CLI commun à 11 drapeaux (`--check` en lecture seule, `--dry-run`, `--force`, `--uninstall`, `--scope {global,workspace}`, `--workspace`, `--target`, `--method {copy,agy}`, `--plugin-root` / `--source`, `--skip-validate` / `--no-validate`, `--strict`) tout en conservant les options de liaison sidecar (`--link`, `--unlink`, `--slug`, `--home`, `--projects-dir`, `--sidecars-dir`, `--source-project-id`, `--project-id`, `--configure-config`, `--no-configure-config`, `--config-path`).

## Sécurité & isolation

- **Feu vert unique :** aucune conversation distante (`dispatch=true`) n'est ouverte ou relancée sans validation humaine préalable (`approved=true`), laquelle couvre également l'éventuel callback retour *one-shot*.
- **Zéro pollution des dépôts :** aucune base ni aucun fichier temporaire n'est déposé dans les workspaces cibles.
- **Anti-boucle fail-closed :** `depth=1` dans les prompts injectés, contrôle `caller_conversation_id` contre les sessions ouvertes par le bridge (`direction='outbound'` uniquement, sans pollution par les callbacks), garde `GRAVITY_BRIDGE_DEPTH >= 1` et zéro repli `new-conversation` sur callback.
- **Filtre secrets & plafonds :** refus en entrée utilisateur (et caviardage en sortie log/erreur) sur les clés AWS, Google, GitHub, `sk-...`, PEM et `Authorization: Bearer ...`, avec plafond à 32 Ko UTF-8 par message.
