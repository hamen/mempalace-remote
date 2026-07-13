# mempalace-remote

Public **remote MCP** front-end for the local MemPalace, so the **Claude app
(phone)** can reach it as a custom connector. Runs **entirely on money-maker** —
the palace data never leaves this desktop. Tailscale Funnel publishes it; no
router ports, no Cloudflare, no Hetzner.

```
Claude app → Anthropic → Tailscale Funnel (:8443) → 127.0.0.1:8789 (this) → mempalace
```

It wraps `mempalace.mcp_server.handle_request` unchanged and puts a minimal
OAuth 2.1 authorization server (metadata + dynamic client registration +
authorization-code + PKCE/S256 + refresh) in front. The human gate is one
passphrase at the login page.

## Setup

1. **Secrets**
   ```bash
   mkdir -p ~/.mempalace/remote
   cp env.example ~/.mempalace/remote/env
   chmod 600 ~/.mempalace/remote/env
   # edit ~/.mempalace/remote/env → set MEMPALACE_REMOTE_PASSPHRASE
   ```

2. **Run** (foreground test)
   ```bash
   ./run.sh
   ```
   or as a service:
   ```bash
   cp mempalace-remote.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now mempalace-remote
   sudo loginctl enable-linger ivan   # survive logout/reboot
   ```

3. **Publish with Tailscale Funnel** on a dedicated port (`:443` is already used
   by an existing tailnet-only serve, so use `:8443`):
   ```bash
   sudo tailscale funnel --bg --https=8443 127.0.0.1:8789
   sudo tailscale funnel status
   ```
   If Funnel isn't enabled for the tailnet yet, the command prints an admin URL
   to grant the `funnel` node attribute (run as the tailnet owner).

4. **Add the connector** in the Claude app / claude.ai → Settings → Connectors →
   Add custom connector → URL:
   ```
   https://your-machine.your-tailnet.ts.net:8443/mcp
   ```
   Claude discovers OAuth, opens the login page → enter the passphrase → done.

## Notes
- Calls to mempalace are serialized (ChromaDB + SQLite KG aren't concurrent-safe).
- OAuth tokens persist in `~/.mempalace/remote/oauth_state.json` (0600).
- This is **model A (tunnel)**: works while money-maker is awake. The local
  Claude Code auto-save hooks are untouched and keep writing the same palace.
