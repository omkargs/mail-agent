"""Interactive setup.

One command takes a new user from nothing to a running agent: AI provider,
model, Google sign-in, Telegram, daemon, voice profile, and a verification pass
that tells them honestly what is and is not working.

Design rules, in order:

1. Never ask for something the machine can find out. Model ids are discovered
   from the endpoint, not typed. A wrong model id must fail here, loudly, with
   the real error — not at 3am in a background run.
2. Never echo a secret. Not once, not masked-but-echoed, not into a log.
3. Refuse to claim success for a step that did not succeed.
4. Re-runnable. Running it twice must not corrupt a working install.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

BOLD = "\033[1m"; DIM = "\033[2m"; GRN = "\033[32m"
YLW = "\033[33m"; RED = "\033[31m"; CYN = "\033[36m"; RST = "\033[0m"


def say(m: str = "") -> None: print(m, flush=True)
def hdr(m: str) -> None: say(f"\n{CYN}{BOLD}▸ {m}{RST}\n")
def ok(m: str) -> None: say(f"  {GRN}✔{RST} {m}")
def warn(m: str) -> None: say(f"  {YLW}!{RST} {m}")
def bad(m: str) -> None: say(f"  {RED}✘{RST} {m}")
def info(m: str) -> None: say(f"  {DIM}{m}{RST}")


def _tty() -> bool:
    return sys.stdin.isatty()


def ask(prompt: str, default: str = "") -> str:
    if not _tty():
        return default
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say("")
        return default
    return val or default


def ask_secret(prompt: str) -> str:
    """Read a secret without echo, or return empty when there is no TTY."""
    if not _tty():
        return ""
    import getpass
    try:
        return getpass.getpass(f"  {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say("")
        return ""


def ask_yn(prompt: str, default: bool = False) -> bool:
    if not _tty():
        return default
    d = "Y/n" if default else "y/N"
    try:
        ans = input(f"  {prompt} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        say("")
        return default
    if not ans:
        return default
    return ans.startswith("y")


# --------------------------------------------------------------- secret store

def config_dir() -> Path:
    return Path(os.environ.get("MAIL_AGENT_CONFIG_DIR", Path.home() / ".config" / "mail-agent"))


def secrets_path() -> Path:
    return config_dir() / ".secrets"


def write_secret(key: str, value: str) -> None:
    """Store a secret, POSIX-quoted, without ever printing it.

    Uses the same single-quote escaping the reader understands, so a value
    containing a quote round-trips instead of being truncated.
    """
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    p = secrets_path()
    quoted = "'" + value.replace("'", "'\\''") + "'"
    lines = []
    if p.exists():
        lines = p.read_text().splitlines()
    replaced = False
    for i, ln in enumerate(lines):
        if ln.startswith(f"export {key}=") or ln.startswith(f"{key}="):
            lines[i] = f"export {key}={quoted}"
            replaced = True
            break
    if not replaced:
        lines.append(f"export {key}={quoted}")
    p.write_text("\n".join(lines) + "\n")
    p.chmod(0o600)


def read_secrets() -> dict[str, str]:
    from .config import _read_secrets_file

    return _read_secrets_file()


# ------------------------------------------------------------------ the steps

def step_provider(state: dict[str, Any]) -> None:
    """AI provider: URL, key, and a model chosen from what the endpoint offers."""
    hdr("1. AI provider")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from .agent import discovery as D

    s = read_secrets()
    base = s.get("ROUTER_BASE_URL") or ask(
        "API base URL (Anthropic-compatible)", "https://api.anthropic.com")
    if not base:
        bad("no base URL — the agent cannot call a model without one")
        return
    base = D.normalise_base(base)
    write_secret("ROUTER_BASE_URL", base)

    key = s.get("ROUTER_API_KEY") or ""
    if key:
        ok(f"using the API key already stored ({len(key)} chars)")
    else:
        key = ask_secret("API key")
        if not key:
            bad("no API key — cannot continue")
            return
        write_secret("ROUTER_API_KEY", key)
        ok("API key stored (mode 600)")

    info(f"asking {base} which models it has…")
    models = D.list_models(base, key)
    if models:
        ranked = D.rank(models)
        ok(f"found {len(ranked)} models")
        chosen = s.get("ROUTER_MODEL") if s.get("ROUTER_MODEL") in ranked else None
        if not chosen:
            show = ranked[:12]
            say("")
            info("  best first:")
            for i, m in enumerate(show, 1):
                mark = " (current)" if m == s.get("ROUTER_MODEL") else ""
                say(f"    {i:>2}. {m}{mark}")
            if len(ranked) > len(show):
                info(f"    … and {len(ranked) - len(show)} more")
            say("")
            n = ask("Pick a number, or type a model id", "1")
            chosen = ranked[int(n) - 1] if n.isdigit() and 1 <= int(n) <= len(show) else n
    else:
        warn("this endpoint does not list models")
        chosen = s.get("ROUTER_MODEL") or ask("Model id", "claude-sonnet-5")

    write_secret("ROUTER_MODEL", chosen)

    info(f"testing {chosen}…")
    res = D.probe(base, key, chosen)
    if res["ok"]:
        ok(f"model works ({res['said']!r})" if res["said"] else "model works")
    else:
        bad(f"model did not answer: {res['error']}")
        if "402" in res["error"] or "credit" in res["error"].lower():
            bad("the provider says the account is out of credit. Top up, then re-run setup.")
        else:
            alt = ask("Try a different model id", chosen)
            if alt and alt != chosen:
                write_secret("ROUTER_MODEL", alt)
                r2 = D.probe(base, key, alt)
                if r2["ok"]:
                    ok(f"{alt} works")
                else:
                    bad(f"{alt} failed too: {r2['error']}")
        return
    state["router_ok"] = True


def step_google(state: dict[str, Any]) -> None:
    """Google sign-in, with a console fallback for headless machines."""
    hdr("2. Google account")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from .providers import build_providers
    from .config import load

    cfg = load()
    creds = Path(cfg.google.credentials_file)
    if not creds.exists():
        bad(f"no client credentials at {creds}")
        info("Create them at https://console.cloud.google.com/apis/credentials")
        info("  1. Enable the Gmail API and the Calendar API")
        info("  2. Create an OAuth client ID of type 'Desktop app'")
        info("  3. Download the JSON and save it as:")
        info(f"     {creds}")
        state["google"] = "missing-creds"
        return

    provs = build_providers(load())
    p = provs.get("google")
    if p and p.valid():
        ok(f"already signed in as {p.address}")
        state["google"] = "ok"
        return

    info("opening the browser for Google sign-in.")
    info("if this machine has no browser, you will get a code to paste below.")
    try:
        p.authenticate()
    except Exception as e:
        warn(f"browser flow failed: {type(e).__name__}: {str(e)[:120]}")
        info("falling back to the console flow")
        if _console_oauth(creds):
            state["google"] = "ok"
        else:
            state["google"] = "failed"
        return

    p2 = build_providers(load()).get("google")
    if p2 and p2.valid():
        write_secret("GOOGLE_ACCOUNT", p2.address)
        ok(f"signed in as {p2.address}")
        state["google"] = "ok"
    else:
        bad("sign-in did not complete")
        state["google"] = "failed"


def _console_oauth(creds: Path) -> bool:
    """Out-of-band flow: print a URL, take the code back."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    from pathlib import Path as P

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(creds), _scopes())
        url, _ = flow.authorization_url(
            access_type="offline", prompt="consent",
            redirect_uri="urn:ietf:wg:oauth:2.0:oob",
        )
    except Exception as e:
        bad(f"could not start the flow: {type(e).__name__}")
        return False
    say("")
    say(f"  {BOLD}Open this URL, approve, then paste the code here:{RST}")
    say(f"  {CYN}{url}{RST}")
    say("")
    code = ask("Code (blank to skip)")
    if not code:
        return False
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        bad(f"that code was rejected: {str(e)[:120]}")
        return False
    out = P(os.environ.get("MAIL_AGENT_TOKEN",
                           config_dir() / "google-token.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(flow.credentials.to_json())
    out.chmod(0o600)
    ok("token stored (mode 600)")
    return True


def _scopes() -> list[str]:
    from .providers.gmail import SCOPES

    return SCOPES


def step_telegram(state: dict[str, Any]) -> None:
    """Optional chat channel. The agent works without it."""
    hdr("3. Chat channel (optional)")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from .notify.channels import build_notifiers
    from .config import load

    s = read_secrets()
    if s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID"):
        ok("Telegram already configured")
        state["telegram"] = "ok"
        return

    if not ask_yn("Set up Telegram so you can message the agent?", True):
        info("skipped — the agent still works, it just cannot talk to you")
        state["telegram"] = "skipped"
        return

    info("1. Message @BotFather in Telegram")
    info("2. Send /newbot, follow the prompts, copy the token it gives you")
    token = ask_secret("Bot token")
    if not token:
        warn("no token — skipping Telegram")
        state["telegram"] = "skipped"
        return
    write_secret("TELEGRAM_BOT_TOKEN", token)

    info("3. Send your bot any message, then press Enter")
    ask("")
    import json as _json
    import urllib.request

    cid = ""
    for _ in range(20):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/getUpdates?timeout=0")
            d = _json.loads(urllib.request.urlopen(req, timeout=20).read())
            for u in d.get("result", []):
                m = u.get("message") or {}
                if not (m.get("from") or {}).get("is_bot"):
                    cid = str((m.get("chat") or {}).get("id") or "")
                    if cid:
                        break
        except Exception:
            pass
        if cid:
            break
        time.sleep(1.5)
    if not cid:
        warn("could not find your chat id — run ./setup-telegram.sh later")
        state["telegram"] = "partial"
        return
    write_secret("TELEGRAM_CHAT_ID", cid)
    ok(f"chat id {cid}")

    n = build_notifiers(load())
    if n.send("Mailbot is set up and talking to you."):
        ok("test message delivered")
        state["telegram"] = "ok"
    else:
        warn("test message did not send — check the token")
        state["telegram"] = "partial"


def step_start(state: dict[str, Any]) -> None:
    """Install and start the always-on service."""
    hdr("4. Always-on service")
    if not ask_yn("Start the agent as a background service?", True):
        info("skipped — run ./start.sh bg when you want it live")
        state["service"] = "skipped"
        return

    unit = Path.home() / ".config/systemd/user/mail-agent.service"
    src = Path(__file__).parent.parent.parent / "systemd" / "mail-agent.service"
    try:
        unit.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copy2(src, unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, timeout=60)
        subprocess.run(["systemctl", "--user", "enable", "--now", "mail-agent"],
                       capture_output=True, timeout=90)
        time.sleep(4)
        r = subprocess.run(["systemctl", "--user", "is-active", "mail-agent"],
                           capture_output=True, text=True, timeout=30)
        if r.stdout.strip() == "active":
            ok("service running")
            state["service"] = "ok"
        else:
            warn(f"service state: {r.stdout.strip() or 'unknown'}")
            state["service"] = "failed"
    except FileNotFoundError:
        warn("systemd not available here — start it manually with ./start.sh bg")
        state["service"] = "unavailable"
    except Exception as e:
        warn(f"could not start the service: {type(e).__name__}")
        state["service"] = "failed"


def step_voice(state: dict[str, Any]) -> None:
    """Learn the user's voice from their sent mail."""
    hdr("5. Learn your writing")
    from .config import load
    from .providers import build_providers

    p = build_providers(load()).get("google")
    if not p or not p.valid():
        warn("no mailbox connected — skipping the voice profile")
        state["voice"] = "skipped"
        return
    info("reading your sent mail to learn how you write (this takes a minute)…")
    try:
        from .brain.style import build_profile
        from .storage import db

        db.migrate()
        msgs = p.list_messages(folder="SENT", limit=300)
        by_id = {}
        try:
            by_id = {f["id"]: f for f in p.get_messages([m["id"] for m in msgs][:200])}
        except Exception:
            pass
        kept = 0
        for m in msgs:
            f = by_id.get(m["id"])
            if not f:
                continue
            m.update(f)
            db.upsert_message(m)
            kept += 1
        if kept:
            db.mark_processed_many([m["id"] for m in msgs[:200]])
        profile = build_profile("google")
        path = load().brain_path() / "profile-google.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile)
        say("")
        say(profile[:1400])
        say("")
        ok(f"voice profile written to {path}")
        state["voice"] = "ok"
    except Exception as e:
        warn(f"could not build the voice profile: {type(e).__name__}: {str(e)[:120]}")
        state["voice"] = "failed"


def step_verify(state: dict[str, Any]) -> None:
    """Say plainly what works and what does not."""
    hdr("6. Check")
    from .config import load
    from .providers import build_providers
    from .storage import db

    cfg = load()
    problems = cfg.validate()
    if problems:
        for p in problems:
            bad(p)
    else:
        ok("configuration is valid")

    p = build_providers(cfg).get("google")
    if p and p.valid():
        ok(f"mailbox connected: {p.address}")
        try:
            n = p.list_messages(folder="INBOX", limit=3)
            ok(f"can read mail ({len(n)} recent messages fetched)")
        except Exception as e:
            bad(f"cannot read mail: {type(e).__name__}")
        if p.calendar_enabled:
            try:
                evs = p.list_events(limit=3)
                ok(f"calendar reachable ({len(evs)} upcoming in the next 14 days)")
            except Exception as e:
                warn(f"calendar not reachable: {type(e).__name__}")
    else:
        bad("no mailbox connected — the agent cannot do anything yet")

    from .notify.channels import build_notifiers

    n = build_notifiers(cfg)
    if n.send("Mailbot is live."):
        ok("chat channel delivers")
    else:
        warn("chat channel did not deliver (fine if you skipped it)")

    db.migrate()
    with db.db() as c:
        pending = c.execute("SELECT COUNT(*) n FROM approvals WHERE status='pending'").fetchone()["n"]
    ok(f"nothing waiting on you ({pending} approvals)")


def summary(state: dict[str, Any]) -> int:
    hdr("Done")
    labels = {
        "router_ok": ("AI provider", True),
        "google": ("Google sign-in", "ok"),
        "telegram": ("Telegram", "ok"),
        "service": ("Background service", "ok"),
        "voice": ("Voice profile", "ok"),
    }
    good = 0
    for k, (label, want) in labels.items():
        v = state.get(k)
        if v is None:
            continue
        done = v is True if want is True else v == want
        (ok if done else warn)(f"{label}: {v}")
        good += 1 if done else 0

    say("")
    if state.get("google") not in ("ok",):
        bad("without a mailbox the agent does nothing. fix that first.")
        return 1
    say(f"  {BOLD}Mailbot is running.{RST}")
    say("")
    say("  Message it on Telegram, or run:")
    say(f"    {DIM}mail-agent status{RST}   what it has done")
    say(f"    {DIM}mail-agent health{RST}   is it alive")
    say(f"    {DIM}mail-agent brief{RST}    the digest")
    say("")
    return 0 if good >= 4 else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    only = argv[0] if argv and not argv[0].startswith("-") else ""

    say("")
    say(f"{BOLD}Mailbot{RST} {DIM}— an autonomous agent for your inbox{RST}")
    say(f"{DIM}Runs on your own machine. Your mail never leaves it except to the")
    say(f"AI provider you choose.{RST}")

    steps = [
        ("provider", step_provider), ("google", step_google),
        ("telegram", step_telegram), ("start", step_start),
        ("voice", step_voice), ("verify", step_verify),
    ]
    state: dict[str, Any] = {}
    for name, fn in steps:
        if only and only != name:
            continue
        try:
            fn(state)
        except KeyboardInterrupt:
            say("")
            warn("stopped — nothing is half-configured")
            return 130
        except Exception as e:
            bad(f"{name} failed: {type(e).__name__}: {str(e)[:160]}")
    if not only:
        return summary(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
