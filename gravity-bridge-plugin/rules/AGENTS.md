# Gravity Bridge

- Sur `/gravity-bridge [workspaces|link|send|read|take|reply|status|done|prune]` uniquement, utiliser le skill `gravity-bridge` et le serveur MCP `gravity-bridge_bridge` (zéro appel proactif hors invocation).
- Exiger une validation humaine explicite avant tout `send_message(..., dispatch=true)`, `reply_thread(..., dispatch=true)` ou `dispatch_thread` (feu vert unique).
- Dans toute conversation ouverte par Gravity Bridge (`depth=1` ou `GRAVITY_BRIDGE_DEPTH >= 1`), ne jamais déclencher de sous-dispatch ; répondre uniquement via `reply_thread(..., dispatch=false)`.
