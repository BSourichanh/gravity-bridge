# Changelog

Toutes les modifications notables de **Gravity Bridge** sont documentées dans ce fichier.
Le format suit [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et ce projet adhère au [Versioning Sémantique](https://semver.org/lang/fr/).

## [Unreleased]

## [0.1.0] - 2026-11-22

### Added
- **Socle SQLite WAL & Sécurité (`core/store.py`, `core/security.py`) :** schéma 4 tables (`workspaces`, `threads`, `messages`, `jobs`), index unique partiel anti-jobs concurrents, normalisation Windows des chemins canoniques, résolution de collisions de slugs, détection/caviardage des secrets (AWS, Google, GitHub, `sk-...`, PEM, Bearer), plafonds UTF-8 (32 Ko) et réconciliation cross-platform des jobs orphelins.
- **Dispatcher détaché & Worker (`core/dispatcher.py`, `scripts/worker.py`) :** instanciation asynchrone détachée Windows/POSIX, exécution via `agy --input-format stream-json --output-format stream-json`, reprise multi-tours `--conversation`, garde fail-closed `GRAVITY_BRIDGE_DEPTH`, annulation propre (`child_pid` puis `pid`) et logs bornés caviardés (256 Ko).
- **Serveur MCP & Skill (`mcp/server.py`, `mcp_config.json`, `skills/gravity-bridge/SKILL.md`, `rules/AGENTS.md`) :** 8 outils JSON-RPC 2.0 stdio (`register_workspace`, `list_workspaces`, `send_message`, `list_inbox`, `read_thread`, `reply_thread`, `dispatch_thread`, `manage_thread`) et protocole de feu vert humain unique, sans aucun hook permanent.
- **Packaging, Installeur & Validateur (`plugin.json`, `assets/logo.svg`, `install.py`, `scripts/validate.py`) :** manifeste officiel 9 champs, installeur cross-platform adaptant `python3` sur la copie cible, validateur `--strict` avec dry-run MCP et suite complète de tests unitaires `unittest`.
