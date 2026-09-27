#!/usr/bin/env bash
# Set or change a secret without it ever appearing in your shell history,
# the transcript, or a log.
#
#   ./set-secret.sh                  # list keys and whether each is set
#   ./set-secret.sh TELEGRAM_BOT_TOKEN
#
# The value is read with echo off and written straight to the secrets file,
# which is chmod 600 before and after. Nothing is echoed back.

set -uo pipefail

CONFIG_DIR="${MAIL_AGENT_CONFIG_DIR:-$HOME/.config/mail-agent}"
SECRETS="$CONFIG_DIR/.secrets"
BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔ %s%s\n' "$GRN" "$*" "$RST"; }
warn() { printf '%s! %s%s\n' "$YLW" "$*" "$RST"; }

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
touch "$SECRETS"
chmod 600 "$SECRETS"

VALID_KEYS=(
  ROUTER_API_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  DISCORD_BOT_TOKEN
  DISCORD_USER_ID
  SLACK_BOT_TOKEN
  SLACK_CHANNEL
  MS_CLIENT_ID
  MS_CLIENT_SECRET
  MS_TENANT_ID
  GOOGLE_IMAP_PASSWORD
  GOOGLE_ACCOUNT
  GOOGLE_DISPLAY_NAME
)

show() {
  say "${BOLD}Secrets in $SECRETS${RST}"
  say ""
  for k in "${VALID_KEYS[@]}"; do
    if grep -qE "^[[:space:]]*export[[:space:]]+${k}=.." "$SECRETS" 2>/dev/null; then
      say "  ${GRN}●${RST} $k ${DIM}(set)${RST}"
    else
      say "  ${DIM}○ $k (empty)${RST}"
    fi
  done
  say ""
  say "${DIM}Run without arguments to see this list.${RST}"
}

KEY="${1:-}"
if [ -z "$KEY" ]; then
  show
  exit 0
fi

if ! printf '%s\n' "${VALID_KEYS[@]}" | grep -qx "$KEY"; then
  warn "unknown key: $KEY"
  say "Valid keys:"
  printf '  %s\n' "${VALID_KEYS[@]}"
  exit 1
fi

if [ ! -t 0 ]; then
  warn "stdin is not a terminal — refusing to read a secret from a pipe."
  say "Run this directly in your shell, not through a tool."
  exit 1
fi

say ""
say "Enter the value for ${BOLD}$KEY${RST}."
say "${DIM}Not echoed, not logged, not saved to history.${RST}"
printf '  > '
read -r -s VAL
printf '\n'

if [ -z "$VAL" ]; then
  warn "empty value — nothing written"
  exit 1
fi

# A newline would let the value append arbitrary shell to this file.
case "$VAL" in
  *$'\n'*|*$'\r'*)
    warn "value contains a newline — refusing. Paste a single line."
    exit 1
    ;;
esac

VAL="${VAL//[$'\n\r']/}"
VAL="${VAL//\'/\'\\\'\'}"

TMP="$(mktemp)"
chmod 600 "$TMP"
if [ -f "$SECRETS" ]; then
  grep -vE "^[[:space:]]*(export[[:space:]]+)?${KEY}=" "$SECRETS" > "$TMP" 2>/dev/null || true
fi
printf "export %s='%s'\n" "$KEY" "$VAL" >> "$TMP"
mv "$TMP" "$SECRETS"
chmod 600 "$SECRETS"

ok "$KEY saved (${#VAL} chars) — mode $(stat -c '%a' "$SECRETS")"
unset VAL
say ""
say "${DIM}Check with: .venv/bin/mail-agent status${RST}"
