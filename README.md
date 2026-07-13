<p align="center">
  <img src="assets/banner.svg" alt="mempalace-remote — your second brain, in your pocket, served from a little server in your house" width="100%">
</p>

<h1 align="center">mempalace-remote 🧠📱</h1>

<p align="center">
  <em>A guarded little door that lets any MCP client — the Claude app on your phone,<br>Codex, Cursor, the lot — read your second brain, while the brain itself<br>never leaves the desk it lives on.</em>
</p>

<p align="center">
  <img alt="remote MCP" src="https://img.shields.io/badge/MCP-remote-8A2BE2?style=for-the-badge">
  <img alt="OAuth 2.1" src="https://img.shields.io/badge/OAuth-2.1%20%2B%20PKCE-2563eb?style=for-the-badge">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white">
  <img alt="Tailscale Funnel" src="https://img.shields.io/badge/Tailscale-Funnel-242424?style=for-the-badge&logo=tailscale&logoColor=white">
  <img alt="Qdrant" src="https://img.shields.io/badge/vectors-Qdrant-DC244C?style=for-the-badge">
</p>

<p align="center">
  <img alt="self-hosted" src="https://img.shields.io/badge/self--hosted-at%20home-2ea44f?style=flat-square">
  <img alt="no cloud" src="https://img.shields.io/badge/no%20cloud-no%20CDN-2ea44f?style=flat-square">
  <img alt="data leaves home" src="https://img.shields.io/badge/data%20leaves%20home-never-8A2BE2?style=flat-square">
</p>

<p align="center">
  <img alt="works with" src="https://img.shields.io/badge/works%20with-Claude%20%C2%B7%20Codex%20%C2%B7%20Cursor%20%C2%B7%20any%20MCP%20client-6f42c1?style=flat-square">
</p>

---

Public **remote MCP** front-end for the local MemPalace, so **any client that
speaks remote MCP** — the Claude app (phone), Codex, Cursor, and the rest — can
reach it as a custom connector. It's plain HTTP MCP + OAuth 2.1, so there's
nothing Claude-specific about it: point any conforming client at the URL. Runs
**entirely on the desktop** — the palace data never leaves it. Tailscale Funnel
publishes it; no router ports, no Cloudflare, no Hetzner. Just a little server in
your house. 🏠

```
📱 any MCP client → 🔐 Tailscale Funnel (:8443) → 127.0.0.1:8789 (this) → 🧠 mempalace
```
> The **Claude app** reaches you via Anthropic's servers (`client → ☁️ Anthropic → Funnel`),
> so the endpoint must be public. **Codex, Cursor & co.** dial the Funnel URL directly.
> Either way the brain stays home.

It wraps `mempalace.mcp_server.handle_request` **without changing a line** and
puts a minimal OAuth 2.1 authorization server (metadata + dynamic client
registration + authorization-code + PKCE/S256 + refresh) in front. No third
party — the server is *both* the resource and its own authorizer. The human gate
is one passphrase at the login page. 🚪

## Setup 🚀

1. **Secrets** 🔑
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

4. **Add the connector** in whatever MCP client you use — same URL for all:
   ```
   https://your-machine.your-tailnet.ts.net:8443/mcp
   ```
   - **Claude app / claude.ai** → Settings → Connectors → Add custom connector
   - **Codex** → add it as a remote MCP server in your config
   - **Cursor** → Settings → MCP → add server (URL / streamable-HTTP)
   - **anything else** that supports remote MCP over HTTP → point it here

   The client discovers OAuth, opens the login page → enter the passphrase → done. ✅

## Notes 📝
- Calls to mempalace are serialized (ChromaDB + SQLite KG aren't concurrent-safe).
- OAuth tokens persist in `~/.mempalace/remote/oauth_state.json` (0600).
- This is **model A (tunnel)**: works while the desktop is awake. The local
  coding-agent auto-save hooks (Claude Code, Codex, Cursor, …) are untouched and
  keep writing the same palace — remote clients just read/write the same brain.
- Backups run hourly (7-day rolling + off-site), and a second always-on box
  watches the public endpoint from the outside — because a machine can't report
  its own death. 🛰️

---

<p align="center"><sub>The datacenter is a house. One brain, one door, one drawer at a time. 🧠</sub></p>
