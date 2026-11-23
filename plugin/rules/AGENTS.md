# Gravity Bridge

- Activer uniquement sur `/gravity-bridge [workspaces|link|send|read|take|reply|status|done|prune]` (skill `gravity-bridge`, MCP `gravity-bridge_bridge`, 0 appel proactif).
- Feu vert humain avant `send_message(..., dispatch=true)`, `reply_thread(..., dispatch=true)` ou `dispatch_thread`.
- Si `depth >= 1` (`GRAVITY_BRIDGE_DEPTH >= 1`), 0 sous-dispatch ; `reply_thread(..., dispatch=false)` seul.
