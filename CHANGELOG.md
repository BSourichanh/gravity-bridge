# Changelog

Toutes les modifications notables de **Gravity Bridge** sont documentées dans ce fichier.
Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et ce projet adhère au [Versioning Sémantique](https://semver.org/lang/fr/).

## [Unreleased]

## [0.3.1] - 2026-11-24

### Added
- **Contrat CLI `install.py` (11/11) :** ajout du drapeau `--no-fix-python` dans `build_parser()` et `install_plugin(..., no_fix_python=False)` pour ignorer `fix_python_cmd` (et le message `[DRY-RUN]` associé) sur la copie installée, avec tests dans `tests/test_install.py`.

### Changed
- **Alignement UX & documentation (`plugin/plugin.json`, `README.md`, `plugin/README.md`) :**
  - Harmonisation des `suggestedPrompts` de `plugin/plugin.json` sur la convention `/cmd — résumé` (`<= 100` caractères).
  - Ajout en tête de `README.md` et `plugin/README.md` de l'encart « Démarrage 2 min & Place dans l'écosystème » (**Palier 3 — Multi-projets**, première commande et liens directs vers `gravity/docs/`).

## [0.3.0] - 2026-11-23

### Added
- **Cure tokens D4 & références (`SKILL.md`, `rules/AGENTS.md`, `AGENTS.md`, `validate.py`) :**
  - Compaction de `plugin/skills/gravity-bridge/SKILL.md` (`<= 3 500 o`) avec extraction des exemples JSON et scénarios multi-tours dans `plugin/skills/gravity-bridge/references/examples.md`.
  - Compaction de `plugin/rules/AGENTS.md` (`<= 420 o`, `<= 5` lignes non vides) et `AGENTS.md` racine (`<= 1 650 o`) sans perte d'invariant.
  - Contrôles D4 dans `plugin/scripts/validate.py` : seuils `600`/`1 000 o`, BOM, CRLF et frontmatter sur `rules/AGENTS.md` ; seuil `<= 3 500 o`, BOM, CRLF et présence de `references/examples.md` sur `SKILL.md` ; seuil `<= 2 500 o`, BOM et CRLF sur `AGENTS.md` racine (lorsque `root.name == "plugin"`).
- **Callback appelant sur réponse cible intermédiaire `close=false` (`core/store.py`, `scripts/sidecar_relay.py`, `mcp/server.py`) :**
  - Enfile un job `direction='callback'` avec fil toujours `open` (et sans poser `callback_dispatched_at`) lorsqu'une réponse cible `close=False` clôt un job outbound `dispatched` actif, tout en préservant le verrouillage anti-boucle (aucun callback sans job outbound actif) et le callback final *one-shot* à la clôture `close=True`.
  - Adaptation du pied de prompt callback dans `build_callback_prompt` lorsque `status=open` (indique la possibilité de relance via `reply_thread(..., dispatch=true)` après arbitrage) et extension des tests (`tests/test_store.py`, `tests/test_sidecar_relay.py`, `tests/test_validate.py`) ainsi que `README.md` et `plugin/README.md`.
- **Contrat CLI commun `install.py` (11 drapeaux) :**
  - Ajout de `--check` (inspection 100 % lecture seule : Python, `agy`, source, cible résolue, état et détection de dérive, zéro écriture disque), `--force` (écrasement cible occupée), `--scope {global,workspace}`, `--workspace PATH`, `--method {copy,agy}` (avec rejet CLI code `2` pour `--method agy` hors `--scope global` ou avec `--target`, et pour `--scope workspace` sans `--workspace`), `--plugin-root` (alias de `--source`), `--skip-validate` (avec conservation de `--no-validate` en alias rétrocompatible) et `--strict`.
  - Extension de `tests/test_install.py` dans `TemporaryDirectory` couvrant tous les nouveaux drapeaux sans toucher `~/.gemini/`, et mise à jour de `README.md` et `plugin/README.md`.

## [0.2.0] - 2026-11-22

### Added
- **Callback retour sécurisé *one-shot* (`core/store.py`, `mcp/server.py`, `scripts/sidecar_relay.py`) :**
  - Schéma SQLite WAL étendu de façon idempotente (`threads.caller_conversation_id`, `threads.notify_caller`, `threads.callback_dispatched_at`, `jobs.direction` dans `'outbound' | 'callback'`).
  - Paramètre optionnel `notify_caller` sur `send_message` et `dispatch_thread` avec résolution *Auto si prêt* (actif automatiquement si `caller_conversation_id` est fourni et que le sidecar appelant est `online` ; avertissement non bloquant si `notify_caller=True` explicite sur sidecar appelant `offline`/`unlinked` ; préservation des métadonnées existantes dans `dispatch_thread`).
  - Déclenchement serveur atomique (`BEGIN IMMEDIATE`) dans `reply_thread(..., close=True, dispatch=False)` lorsqu'il est émis par `to_workspace` sur un fil approuvé avec `notify_caller=1`, `caller_conversation_id != target_conversation_id`, `callback_dispatched_at IS NULL` et aucun job en attente ; retour non bloquant `callback_status="skipped_sidecar_offline"` / `"skipped_sidecar_unlinked"` si le sidecar appelant est indisponible.
  - Exécution dédiée dans `sidecar_relay.py` : résolution de `cwd` depuis `job["target_workspace"]`, résolution stricte de `caller_conversation_id`, préfixe `[GRAVITY-BRIDGE | CALLBACK | thread=<id> | from=<to_workspace> | status=done | depth=1]`, zéro repli `new-conversation` en cas d'échec, et complétion directe via `complete_job` sans appeler `mark_job_dispatched`, sans altérer `threads.target_conversation_id` et sans polluer `list_target_conversation_ids()`.

### Changed
- Synchronisation SemVer `0.2.0` (`plugin/plugin.json`, `plugin/mcp/server.py`, `README.md`, `plugin/README.md`) et mise à jour du skill `/gravity-bridge` et de `plugin/rules/AGENTS.md`.

## [0.1.0] - 2026-11-22

### Added
- **Socle SQLite WAL & Sécurité (`core/store.py`, `core/security.py`) :** schéma 4 tables (`workspaces`, `threads`, `messages`, `jobs`) avec heartbeat sidecar (`sidecar_id`, `sidecar_pid`, `sidecar_last_seen_at`, `project_id`), résolution centralisée `home` > `$GRAVITY_BRIDGE_HOME` > `$GRAVITY_HOME/bridge` > `~/.gemini/gravity/bridge/`, migration idempotente depuis le schéma CLI initial, index unique partiel anti-jobs concurrents, normalisation Windows des chemins canoniques, détection/caviardage des secrets (AWS, Google, GitHub, `sk-...`, PEM, Bearer), plafonds UTF-8 (32 Ko), anti-boucle (`caller_conversation_id`, `GRAVITY_BRIDGE_DEPTH`) et réconciliation des jobs `queued`/`claimed` orphelins.
- **Gestionnaire & Relais Sidecar `agentapi` (`core/sidecar_manager.py`, `scripts/sidecar_relay.py`) :** génération de micro-sidecars dédiés par projet (`~/.gemini/config/sidecars/gravity-bridge-<slug>/sidecar.json`), création/réutilisation d'un projet sidecar dédié `<nom> (Bridge)` dans `~/.gemini/config/projects/*.json` (miroir de `projectResources`, `settings` et `permissionGrants` du projet utilisateur sans jamais lier ni masquer le projet utilisateur lorsqu'il est vide), réparation automatique des projets utilisateur pollués par `"sidecar"`, activation atomique dans `config.json` avec `"schemaVersion": 1`, boucle heartbeat + verrouillage atomique SQLite, création et poursuite de vraies conversations dans **Antigravity App** via `agentapi new-conversation` et `agentapi send-message` (avec résolution directe sous Windows hors `cmd.exe`, repli sur `new-conversation` si conversation absente et logs bornés caviardés à 256 Ko).
- **Serveur MCP & Skill (`mcp/server.py`, `mcp_config.json`, `skills/gravity-bridge/SKILL.md`, `rules/AGENTS.md`) :** 9 outils JSON-RPC 2.0 stdio (`register_workspace`, `link_workspace`, `list_workspaces`, `send_message`, `list_inbox`, `read_thread`, `reply_thread`, `dispatch_thread`, `manage_thread`), fail-fast si sidecar cible hors ligne/non lié, auto-complétion des jobs `dispatched` sur `reply_thread` et protocole de feu vert humain unique, sans aucun hook permanent.
- **Packaging, Contrat Module, Installeur & Validateur (`plugin/`, `plugin/plugin.json`, `plugin/config/module.json`, `assets/logo.svg`, `install.py`, `scripts/validate.py`) :** arborescence standardisée sous `plugin/`, manifeste officiel 9 champs, contrat module v1 (`id: "bridge"`), installeur cross-platform (`PLUGIN_SOURCE = plugin/`) avec `--link`/`--unlink`/`--project-id` et adaptation `python3` sur la copie cible, validateur `--strict` (avec contrôle `config/module.json` v1 et dry-run MCP) et suite complète de tests unitaires `unittest`.
