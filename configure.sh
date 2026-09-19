#!/usr/bin/env bash
# First-run setup. Run this yourself in a terminal: it asks for the three secrets with silent prompts
# (nothing is echoed or logged), writes them to .env (mode 600), runs the self-test, then offers to start the stack.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo ".env not found in $PWD" >&2; exit 1; }
umask 077

set_kv() { # set_kv KEY VALUE  (value passed via environment, never argv)
  KEY="$1" VAL="$2" python3 - <<'PY'
import os, pathlib, re
k, v = os.environ["KEY"], os.environ["VAL"]
p = pathlib.Path(".env"); t = p.read_text()
pat = re.compile(rf"^#?\s*{re.escape(k)}=.*$", re.M)
t = pat.sub(lambda _: f"{k}={v}", t, count=1) if pat.search(t) else t + f"\n{k}={v}\n"
p.write_text(t)
PY
}

read -rp "iCloud address / Apple ID (e.g. you@icloud.com): " ICLOUD_USERNAME
read -rp "Display name for outgoing mail: " ICLOUD_DISPLAY_NAME
echo "Create the app-specific password at https://account.apple.com > Sign-In and Security > App-Specific Passwords."
read -rsp "App-specific password (xxxx-xxxx-xxxx-xxxx): " ICLOUD_APP_PASSWORD; echo
read -rsp "Owner password for the approval page (12+ chars; Enter to generate one): " OWNER; echo
if [ -z "$OWNER" ]; then
  OWNER="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  echo "Generated owner password (shown once, save it in your password manager): $OWNER"
fi
[ "${#OWNER}" -ge 12 ] || { echo "Owner password must be at least 12 characters." >&2; exit 1; }

set_kv ICLOUD_USERNAME "$ICLOUD_USERNAME"
set_kv ICLOUD_DISPLAY_NAME "$ICLOUD_DISPLAY_NAME"
set_kv ICLOUD_APP_PASSWORD "$ICLOUD_APP_PASSWORD"
set_kv MCP_OWNER_PASSWORD "$OWNER"
chmod 600 .env
unset ICLOUD_APP_PASSWORD OWNER

PUBLIC_URL="$(grep -E '^MCP_PUBLIC_URL=' .env | cut -d= -f2-)"
case "$PUBLIC_URL" in ""|*example.com*) echo "Set MCP_PUBLIC_URL in .env to your real public https address first." >&2; exit 1;; esac

echo; echo "== Self-test against real iCloud (sends nothing) =="
docker image inspect icloud-mcp:local >/dev/null 2>&1 || docker build -t icloud-mcp:local .
if ! docker run --rm --env-file .env icloud-mcp:local python -m icloud_mcp.selftest; then
  echo; echo "Self-test failed. Fix .env (login-name overrides: IMAP_USERNAME, SMTP_USERNAME, CALDAV_USERNAME) and re-run ./configure.sh or the docker run line above." >&2
  exit 1
fi

echo
read -rp "Start the connector now (icloud-mcp + tunnel to $PUBLIC_URL)? [y/N] " go
if [[ "$go" =~ ^[Yy]$ ]]; then
  docker compose up -d --build
  sleep 5
  docker compose ps
  echo; echo "Public check:"; curl -s -o /dev/null -w "$PUBLIC_URL/healthz -> %{http_code}\n" "$PUBLIC_URL/healthz" || true
  echo; echo "Next: Claude > Settings > Connectors > Add custom connector > $PUBLIC_URL/mcp"
  echo "Sending is OFF (ALLOW_SEND=false). To enable: set ALLOW_SEND=true in .env, then: docker compose up -d"
fi
