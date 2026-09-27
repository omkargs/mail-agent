#!/usr/bin/env bash
# mail-agent setup. Collects credentials, installs, and wires systemd.
#
# Secrets are written to ~/.config/mail-agent/.secrets with mode 600 and are
# never echoed back, never logged, and never committed.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${MAIL_AGENT_CONFIG_DIR:-$HOME/.config/mail-agent}"
SECRETS="$CONFIG_DIR/.secrets"
CONFIG="$CONFIG_DIR/config.json"
VENV="$REPO/.venv"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔ %s%s\n' "$GRN" "$*" "$RST"; }
warn() { printf '%s! %s%s\n' "$YLW" "$*" "$RST"; }
hdr()  { printf '\n%s▸ %s%s\n\n' "$BOLD" "$*" "$RST"; }

# Secret input: visible=false, no default echo, no history.
# Returns non-zero when stdin is not a TTY, so callers can fail loudly
# instead of silently writing an empty credential.
ask_secret() {
  local prompt="$1" val
  if [ ! -t 0 ]; then
    printf '\n' >&2
    return 1
  fi
  read -r -s -p "$(printf '%s' "$prompt") " val
  printf '\n'
  printf '%s' "$val"
}

# setkv that reports failure. Returns non-zero if the value was empty, so a
# skipped step can never print a green checkmark.
setkv_required() {
  local key="$1" val="$2"
  if [ -z "$val" ]; then
    warn "no value given for $key — not written"
    return 1
  fi
  setkv "$key" "$val"
  return 0
}

ask_plain() {
  local prompt="$1" def="${2:-}" val
  if [ -n "$def" ]; then
    read -r -p "$(printf '%s' "$prompt") [$def]: " val
    printf '%s' "${val:-$def}"
  else
    read -r -p "$(printf '%s' "$prompt"): " val
    printf '%s' "$val"
  fi
}

ask_yn() {
  local prompt="$1" def="${2:-n}" val
  [ "$def" = "y" ] && printf '%s(y/N)%s' "$DIM" "$RST" || printf '%s(Y/n)%s' "$DIM" "$RST"
  read -r -p "$prompt " val
  case "${val:-$def}" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

setkv() {
  # setkv <KEY> <value>  — idempotent upsert into the secrets file.
  #
  # The value is NEVER passed through sed. A previous version escaped it with
  # sed and then interpolated the result into a second sed replacement, so a
  # key containing a backslash, $, `, or & produced
  # "unterminated `s' command" and setup died with the credential half-written.
  #
  # Instead: strip control characters in pure bash, then use shell single
  # quotes for the value. Single-quoted values need no escaping except a
  # literal single quote, handled below.
  local key="$1" val="$2" tmp
  [ -z "$val" ] && return 0

  # Drop newlines/carriage returns and any other control chars. A credential
  # never legitimately contains these, and a stray newline would let an
  # attacker append arbitrary shell to this file.
  val="${val//[$'\n\r']/}"
  # Escape embedded single quotes the POSIX way: end quote, escaped quote.
  val="${val//\'/\'\\\'\'}"

  mkdir -p "$CONFIG_DIR"
  touch "$SECRETS"
  tmp="$(mktemp)"
  chmod 600 "$tmp"

  if [ -f "$SECRETS" ]; then
    grep -vE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$SECRETS" > "$tmp" 2>/dev/null || true
  fi
  printf "export %s='%s'\n" "$key" "$val" >> "$tmp"
  mv "$tmp" "$SECRETS"
  chmod 600 "$SECRETS"
}

say "${BOLD}mail-agent setup${RST}"
say "${DIM}$REPO${RST}"

# ---------------------------------------------------------------- python
hdr "Python environment"
if [ ! -d "$VENV" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV"
  else
    python3 -m venv "$VENV"
  fi
fi
ok "venv at $VENV"
if command -v uv >/dev/null 2>&1; then
  VIRTUAL_ENV="$VENV" uv pip install -e "$REPO" -q
else
  "$VENV/bin/pip" install -e "$REPO" -q
fi
ok "package installed"

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

# ---------------------------------------------------------------- router
hdr "AI provider (router.bynara.id)"
setkv "ROUTER_BASE_URL" "$(ask_plain 'Base URL' 'https://router.bynara.id')"
setkv "ROUTER_MODEL" "$(ask_plain 'Model combo' 'combo/claude2mail')"
k="$(ask_secret 'Router API key (input hidden)')"
setkv "ROUTER_API_KEY" "$k"
[ -n "$k" ] && ok "router key stored" || warn "no router key — the agent cannot run yet"

# ---------------------------------------------------------------- google
hdr "Google (Gmail + Calendar)"
if ask_yn "Set up a Google account?" y; then
  creds="$CONFIG_DIR/google-credentials.json"
  say "${DIM}Download an OAuth Desktop client from:"
say "  https://console.cloud.google.com/apis/credentials"
say "  Client ID → Download JSON, then give the path here."
  src="$(ask_plain 'Path to credentials JSON' "$creds")"
  if [ -f "$src" ]; then
    cp -f "$src" "$creds"; chmod 600 "$creds"
    setkv "GOOGLE_CREDENTIALS" "$creds"
    setkv "GOOGLE_ACCOUNT" "$(ask_plain 'Gmail address' "$HOME@gmail.com")"
    setkv "GOOGLE_DISPLAY_NAME" "$(ask_plain 'Display name' "${USER}")"
    if ask_yn "Let the agent manage Google Calendar?" y; then
      setkv "GOOGLE_CALENDAR_ENABLED" "true"
    else
      setkv "GOOGLE_CALENDAR_ENABLED" "false"
    fi
    ok "google configured"
  else
    warn "no file at $src — skipping Google"
  fi
fi

# ---------------------------------------------------------------- microsoft
hdr "Microsoft 365 / Outlook"
if ask_yn "Set up a Microsoft account?" n; then
  setkv "MS_TENANT_ID" "$(ask_plain 'Tenant ID' 'common')"
  setkv "MS_CLIENT_ID" "$(ask_plain 'Application (client) ID')"
  cs="$(ask_secret 'Client secret (input hidden)')"
  setkv "MS_CLIENT_SECRET" "$cs"
  setkv "MS_ACCOUNT" "$(ask_plain 'Email address')"
  setkv "MS_DISPLAY_NAME" "$(ask_plain 'Display name' "${USER}")"
  if ask_yn "Let the agent manage Outlook Calendar?" n; then
    setkv "MS_CALENDAR_ENABLED" "true"
  else
    setkv "MS_CALENDAR_ENABLED" "false"
  fi
  ok "microsoft configured (device-code auth on first run)"
fi

# ---------------------------------------------------------------- discord
hdr "Discord (approvals + brief)"
if ask_yn "Set up Discord?" y; then
  say "${DIM}Create an app at https://discord.com/developers/applications"
  say "  Bot → enable MESSAGE CONTENT INTENT. Invite with 'bot' scope and 'Send Messages'."
  tok="$(ask_secret 'Bot token (input hidden)')"
  if setkv_required "DISCORD_BOT_TOKEN" "$tok"; then
    setkv "DISCORD_USER_ID" "$(ask_plain 'Your Discord user ID (for DM)')"
    ok "discord configured"
  else
    warn "discord skipped — approvals will have nowhere to go. Rerun setup.sh to add it."
  fi
fi

hdr "Telegram (optional)"
if ask_yn "Set up Telegram?" n; then
  tok="$(ask_secret 'Bot token (input hidden)')"
  if setkv_required "TELEGRAM_BOT_TOKEN" "$tok"; then
    setkv "TELEGRAM_CHAT_ID" "$(ask_plain 'Your chat ID')"
    ok "telegram configured"
  else
    warn "telegram skipped"
  fi
fi

hdr "Slack (optional)"
if ask_yn "Set up Slack?" n; then
  tok="$(ask_secret 'Bot token (input hidden)')"
  if setkv_required "SLACK_BOT_TOKEN" "$tok"; then
    setkv "SLACK_CHANNEL" "$(ask_plain 'Channel ID')"
    ok "slack configured"
  else
    warn "slack skipped"
  fi
fi

# ---------------------------------------------------------------- policy
hdr "Send policy"
if ask_yn "Allow the agent to send WITHOUT your approval, for contacts you approve? (recommended)" y; then
  setkv "AGENT_SEND_MODE" "auto"
  setkv "GOOGLE_AUTO_SEND" "true"
  setkv "MS_AUTO_SEND" "true"
  say "${DIM}You still approve each contact once via: mail-agent contacts --approve EMAIL${RST}"
else
  setkv "AGENT_SEND_MODE" "never"
  setkv "GOOGLE_AUTO_SEND" "false"
  setkv "MS_AUTO_SEND" "false"
  warn "agent will only ever create drafts"
fi
setkv "AGENT_SCAN_INTERVAL" "$(ask_plain 'Scan interval seconds' '300')"
setkv "AGENT_DAILY_TOKEN_CAP" "$(ask_plain 'Daily token cap' '2000000')"
setkv "AGENT_BRIEF_HOUR" "$(ask_plain 'Hour for morning brief (0-23)' '7')"

# ---------------------------------------------------------------- config.json
hdr "Writing config"
cat > "$CONFIG" <<JSON
{
  "enabled_accounts": [],
  "auto_send_contacts": [],
  "never_auto_send": [],
  "escalation_keywords": [
    "invoice","payment","wire","contract","legal","attorney","bank","salary",
    "offer","termination","medical","passport","ssn","password","otp","verify",
    "account number","recovery code","2fa","mfa"
  ]
}
JSON
chmod 600 "$CONFIG"
ok "wrote $CONFIG"

# ---------------------------------------------------------------- auth
hdr "Authentication"
if "$VENV/bin/mail-agent" check; then
  ok "router reachable"
else
  warn "router check failed — fix ROUTER_API_KEY before running the agent"
fi

if ask_yn "Run the browser auth flow now?" y; then
  "$VENV/bin/mail-agent" auth || warn "auth did not complete"
fi

if ask_yn "Build the Email Brain from your sent mail now? (recommended)" y; then
  "$VENV/bin/mail-agent" brain || warn "brain build failed"
fi

# ---------------------------------------------------------------- systemd
hdr "Background service"
if command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  sed -e "s|@REPO@|$REPO|g" -e "s|@VENV@|$VENV|g" \
      "$REPO/systemd/mail-agent.service" > "$HOME/.config/systemd/user/mail-agent.service"
  systemctl --user daemon-reload
  # Do not start a service that will crash-loop. Only start it if the
  # prerequisites are actually present.
  CAN_START=1
  [ -z "$(grep -oE '^export ROUTER_API_KEY="[^"]+"' "$SECRETS" 2>/dev/null | grep -v '=""')" ] && CAN_START=0
  if [ ! -f "$CONFIG_DIR/google-token.json" ] && [ ! -f "$CONFIG_DIR/ms-token.json" ]; then
    CAN_START=0
  fi

  if [ "$CAN_START" -eq 0 ]; then
    warn "service installed but NOT started — router key or account login is missing."
    warn "starting it now would crash-loop. Finish setup, then:"
    say "    systemctl --user start mail-agent"
  elif ask_yn "Start the agent on login?" y; then
    systemctl --user enable --now mail-agent.service
    ok "mail-agent.service running"
  else
    warn "installed but not started — systemctl --user start mail-agent"
  fi
else
  warn "systemd not found; run the daemon manually: $VENV/bin/mail-agent daemon"
fi

hdr "Done"
cat <<EOF
Next steps:
  1. Review your voice profile:   cat $REPO/brain/profile-google.md
  2. Approve a contact:           $VENV/bin/mail-agent contacts --approve someone@example.com
  3. Watch the agent work:        $VENV/bin/mail-agent scan --capped    # drafts only, no sends
  4. Go live:                     systemctl --user start mail-agent
  5. Morning brief:               $VENV/bin/mail-agent brief

Config:   $CONFIG
Secrets:  $SECRETS  (mode 600)
EOF
