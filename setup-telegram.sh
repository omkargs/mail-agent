#!/usr/bin/env bash
# Finish Telegram setup: find your chat ID and verify the bot can message you.
#
# Run ./set-secret.sh TELEGRAM_BOT_TOKEN first.
# Then send your bot any message (e.g. "hi") on Telegram, and run this.

set -uo pipefail

CONFIG_DIR="${MAIL_AGENT_CONFIG_DIR:-$HOME/.config/mail-agent}"
SECRETS="$CONFIG_DIR/.secrets"
BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔ %s%s\n' "$GRN" "$*" "$RST"; }
warn() { printf '%s! %s%s\n' "$YLW" "$*" "$RST"; }

getval() {
  # Read a key from the secrets file without sourcing the whole file.
  grep -E "^[[:space:]]*export[[:space:]]+$1=" "$SECRETS" 2>/dev/null \
    | head -1 | sed -E "s/^[[:space:]]*export[[:space:]]+$1=(.*)$/\1/" \
    | sed -E "s/^'(.*)'$/\1/" | sed -E "s/\\\\'/'/g"
}

TOKEN="$(getval TELEGRAM_BOT_TOKEN)"
if [ -z "$TOKEN" ]; then
  warn "no TELEGRAM_BOT_TOKEN set."
  say "  Run: ./set-secret.sh TELEGRAM_BOT_TOKEN"
  exit 1
fi

say "${BOLD}1. Checking the bot${RST}"
ME="$(curl -s --max-time 15 "https://api.telegram.org/bot${TOKEN}/getMe")"
BOTNAME="$(printf '%s' "$ME" | .venv/bin/python -c 'import sys,json; d=json.load(sys.stdin); print(d.get("result",{}).get("username","")) if d.get("ok") else print("")' 2>/dev/null)"
if [ -z "$BOTNAME" ]; then
  warn "bot token rejected by Telegram."
  say "  Double-check the token. It looks like: 123456789:AAExxxxxxxxxxxxxxxxxxxxxxx"
  exit 1
fi
ok "token valid — bot is @${BOTNAME}"

say ""
say "${BOLD}2. Finding your chat ID${RST}"
say "${DIM}Send your bot any message on Telegram now (just 'hi'). Retrying for 60s...${RST}"

CHAT=""
for _ in $(seq 1 20); do
  UP="$(curl -s --max-time 15 "https://api.telegram.org/bot${TOKEN}/getUpdates")"
  CHAT="$(printf '%s' "$UP" | .venv/bin/python -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
for u in reversed(d.get("result", [])):
    m = u.get("message") or u.get("edited_message")
    if m and m.get("from", {}).get("is_bot") is not True:
        print(m["chat"]["id"]); break
' 2>/dev/null)"
  [ -n "$CHAT" ] && break
  sleep 3
done

if [ -z "$CHAT" ]; then
  warn "no message from you yet."
  say "  1. Open Telegram, find @${BOTNAME}"
  say "  2. Send it any message"
  say "  3. Re-run: ./setup-telegram.sh"
  exit 1
fi

TMP="$(mktemp)"; chmod 600 "$TMP"
grep -vE "^[[:space:]]*(export[[:space:]]+)?TELEGRAM_CHAT_ID=" "$SECRETS" > "$TMP" 2>/dev/null || true
printf "export TELEGRAM_CHAT_ID='%s'\n" "$CHAT" >> "$TMP"
mv "$TMP" "$SECRETS"; chmod 600 "$SECRETS"
ok "chat ID saved"

say ""
say "${BOLD}3. Sending a test message${RST}"
R="$(curl -s --max-time 15 -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
      -d "chat_id=${CHAT}" \
      -d "text=mail-agent connected. Approvals and the morning brief will land here.")"
if printf '%s' "$R" | grep -q '"ok":true'; then
  ok "message delivered — check Telegram"
else
  warn "send failed. Response:"
  printf '%s\n' "$R" | head -3
fi

say ""
say "${DIM}No token or chat ID was printed. Both are in $SECRETS (mode 600).${RST}"
