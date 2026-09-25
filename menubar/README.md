# iCloud MCP Control

A small menu bar app for the Mac that runs your icloud-mcp server. One click shows whether the server is up, how many
tools it offers and whether anything needs attention. It is also where you pause the server, restart it and change its
credentials.

- **At a glance:** the menu bar icon changes shape when the server is paused, stopped or has a problem.
- **Pause:** one switch stops every tool from answering, without disconnecting any app. Diagnostics keep working.
- **Restart and stop:** the server, the Mac helper and your tunnel, one at a time or all together.
- **Credentials:** change the owner passcode, replace the iCloud app-specific password (it is tested with iCloud before
  anything is saved), or sign out every connected app.
- **Health:** each iCloud service is checked when you open the menu, and problems are explained in plain words.

It needs macOS 26 or later and a server installed with launchd (as the repository's Mac setup does).

## Turn on the admin API

The app talks to the server's admin API. It is off until you turn it on:

1. Add `ADMIN_PORT=8002` to the server's settings (the `.env` file) and restart the server.
2. The server creates `admin-token` in its data folder (DATA_DIR), readable only by you.

The admin API listens on 127.0.0.1 only, on its own port, so a tunnel that forwards the server's public port never
reaches it. Every request needs the token.

## Build

```bash
./build.sh
```

This builds `build/iCloud MCP Control.app`, signed for your own Mac. Move it to Applications and open it. The first time,
the app looks for your server's launch agents in `~/Library/LaunchAgents` and for its data folder; if it cannot find the
data folder, choose it in Settings, Connection.

To publish a build for other people, sign it with a Developer ID and notarise it instead of the ad hoc signature.

## What it changes

- Pause writes the file `paused` in the data folder; resume removes it.
- A new passcode or app-specific password is saved in `overrides.json` in the data folder (mode 600) and takes precedence
  over the environment. The server restarts to use it.
- Stop unloads the launch agents (`launchctl bootout`); start loads them again (`launchctl bootstrap`).

The app keeps its own settings in its preferences and writes no other files.
