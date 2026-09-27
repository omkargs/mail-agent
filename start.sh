#!/usr/bin/env bash
# Start / stop / inspect the always-on mail agent.
#
# systemd is the real deployment. This script is the foreground/manual path:
# useful over SSH, for debugging, and when you want to watch what it does.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv"
CLI="$VENV/bin/mail-agent"
UNIT="mail-agent.service"

BOLD=$'\033[1m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✔ %s%s\n' "$GRN" "$*" "$RST"; }
warn() { printf '%s! %s%s\n' "$YLW" "$*" "$RST"; }
hdr()  { printf '\n%s▸ %s%s\n\n' "$BOLD" "$*" "$RST"; }

usage() {
  cat <<EOF
usage: ./start.sh <command>

  start       Start the agent in the foreground (logs stream live)
  bg          Start it as a background daemon (detached, logs to a file)
  stop        Stop the background daemon
  status      Is it running? What did it do?
  logs        Tail the log
  install     Install + enable the systemd unit (survives reboot)
  install-start  Install it and start now (refuses if config is incomplete)
  uninstall   Remove the systemd unit
  check       Verify the AI router is reachable
EOF
}

[ -x "$CLI" ] || { warn "not installed. Run ./setup.sh first."; exit 1; }

cmd="${1:-}"
case "$cmd" in
  start)
    hdr "mail-agent — foreground (Ctrl-C to stop)"
    exec "$CLI" daemon
    ;;

  bg)
    mkdir -p "$HOME/.local/share/mail-agent"
    LOG="$HOME/.local/share/mail-agent/daemon.log"
    PIDF="$HOME/.local/share/mail-agent/daemon.pid"
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      warn "already running as PID $(cat "$PIDF")"
      exit 0
    fi
    nohup "$CLI" daemon >> "$LOG" 2>&1 &
    echo $! > "$PIDF"
    # A daemon that is unconfigured exits quickly. Give it time to fail, then
    # confirm it is still alive AND has passed warmup (heartbeat file written).
    BEAT="$HOME/.local/share/mail-agent/daemon.json"
    rm -f "$BEAT"
    for _ in $(seq 1 15); do
      sleep 1
      kill -0 "$(cat "$PIDF")" 2>/dev/null || break
      [ -f "$BEAT" ] && [ -s "$BEAT" ] && break
    done
    if ! kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      warn "exited immediately — last lines:"
      tail -6 "$LOG" | sed 's/^/    /'
      rm -f "$PIDF"
      exit 1
    fi
    if [ ! -s "$BEAT" ]; then
      warn "started but did not report healthy — it may be unconfigured:"
      tail -6 "$LOG" | sed 's/^/    /'
      say ""
      say "Check with: ./start.sh status"
      exit 1
    fi
    ok "started (PID $(cat "$PIDF")), logging to $LOG"
    ;;

  stop)
    PIDF="$HOME/.local/share/mail-agent/daemon.pid"
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
      kill "$(cat "$PIDF")" && rm -f "$PIDF" && ok "stopped"
    else
      warn "not running"
    fi
    ;;

  status)  exec "$CLI" health ;;
  check)   exec "$CLI" check ;;
  logs)
    LOG="$HOME/.local/share/mail-agent/daemon.log"
    if [ -f "$LOG" ]; then
      exec tail -f "$LOG"
    fi
    warn "no $LOG yet. Use './start.sh start' or journalctl for the systemd unit."
    exec journalctl --user -u "$UNIT" -f
    ;;

  install)
    hdr "Installing systemd unit"
    if ! systemctl --user list-unit-files 2>/dev/null | grep -q "$UNIT"; then
      mkdir -p "$HOME/.config/systemd/user"
      sed -e "s|@REPO@|$REPO|g" -e "s|@VENV@|$VENV|g" \
        "$REPO/systemd/mail-agent.service" > "$HOME/.config/systemd/user/$UNIT"
    fi
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT"
    say "${BOLD}Installed. It will NOT start yet if credentials are missing.${RST}"
    say "Run './start.sh install-start' after setup to launch it."
    ok "unit enabled"
    ;;

  install-start)
    # Only start if configuration is actually present.
    problems="$("$CLI" status 2>/dev/null | sed -n '/! /p')"
    if [ -n "$problems" ]; then
      warn "not starting — configuration is incomplete:"
      printf '%s\n' "$problems" | sed 's/^/    /'
      say "Fix these, then: ./start.sh install-start"
      exit 1
    fi
    systemctl --user reset-failed "$UNIT" 2>/dev/null
    systemctl --user enable --now "$UNIT"
    sleep 2
    if systemctl --user is-active --quiet "$UNIT"; then
      ok "$UNIT is running"
    else
      warn "did not start:"
      journalctl --user -u "$UNIT" -n 15 --no-pager
      exit 1
    fi
    ;;

  uninstall)
    systemctl --user disable --now "$UNIT" 2>/dev/null
    rm -f "$HOME/.config/systemd/user/$UNIT"
    systemctl --user daemon-reload
    ok "unit removed"
    ;;

  ""|-h|--help|help) usage ;;
  *) say "unknown command: $cmd"; usage; exit 1 ;;
esac
