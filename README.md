# mail-agent

Autonomous multi-provider inbox agent. Reads your mail, sorts it, labels it, drafts replies in
your voice, and sends without asking — except when it decides you're actually needed.

Replaces the `axren-mail-mcp` MCP server and the `claude-2-mail` project. Mail-only, plus
calendar driven from mail.

## What it does

- **Email Brain** — mines your sent mail to learn your tone, greeting/sign-off patterns, vocabulary,
  and who you actually correspond with. Produces an editable profile.
- **Continuous learning** — every send is compared against what you actually sent. Corrections
  accumulate and the profile converges on your real voice.
- **Auto mode** — the agent self-checks each send. Clean and routine → sends and tells you after.
  Money, legal, credential, new-sender, attachment, or anything ambiguous → asks you first.
- **Morning brief** — what arrived overnight, what was auto-filed, what needs you.
- **Quiet threads** — outbound messages that were never answered, before they go cold.
- **Batch reply** — handles N similar emails (scheduling, recruiters, logistics) in one run.
- **Skills** — save a routine once, replay it forever.
- **Multi-account** — Gmail and Microsoft 365/Outlook through one interface.
- **Calendar** — creates events from mail content. Never deletes.

## Install

```bash
./setup.sh
```

Asks for credentials, installs, authenticates, builds the brain, and offers a systemd service.

Secrets land in `~/.config/mail-agent/.secrets` (mode 600). They are never logged or committed.

## Use

```bash
mail-agent status                  # config + run state
mail-agent check                   # verify the AI router answers
mail-agent auth                    # browser / device-code login
mail-agent brain                   # rebuild the voice profile
mail-agent scan                    # one pass over new mail
mail-agent scan --capped           # drafts only, no sends
mail-agent daemon                  # run continuously
mail-agent brief                   # morning digest
mail-agent quiet                   # threads going cold
mail-agent voice --log 5           # what the agent has learned about your voice
mail-agent contacts                # who may get unattended replies
mail-agent contacts --approve a@b.com
mail-agent approve <id>            # approve or --deny a queued send
mail-agent skill save NAME --instructions "..."
```

## Always-on

`mail-agent daemon` under systemd (`Restart=always`) is the always-on runtime. What it does:

- **Push, not poll.** IMAP IDLE (Gmail) parks a connection and the server pushes the moment mail
  arrives — sub-second detection, zero cost while idle. Falls back to interval polling if no
  app password is set, and the poll is a safety net either way so a broken IDLE never means
  missed mail.
- **Auth once.** Providers authenticate at boot, not every cycle. A token refresh mid-send can't
  kill a run.
- **Backs off.** Exponential with jitter, capped at 30 min. A dead router isn't hammered. After
  8 consecutive failures it stops and tells you, rather than spinning forever.
- **Never misses the brief.** Fires at `brief_hour`, and catches up if the machine was asleep.
- **Loud failure.** Health state is persisted, a watchdog thread flags a wedged loop, and dead
  auth is reported to your chat once, not silently retried.

```bash
mail-agent health        # is it alive, what did it do, how much budget is left
systemctl --user status mail-agent
journalctl --user -u mail-agent -f
```

To get push detection, add a Gmail **app password** (Settings → Security → App passwords →
Mail). This is separate from the OAuth login used for the API; IMAP can't use an OAuth token.
It stays optional — without it you poll on the configured interval.

## Send policy

`AGENT_SEND_MODE` is `auto` or `never`. Under `auto`, a send is unattended only when **all** hold:

- the account has auto-send enabled
- the recipient is on your approved contact list
- the recipient is not on the never-send list
- no prompt-injection signal is present
- no escalation keyword (invoice, wire, contract, password, otp, …)
- no attachments
- the body is long enough to have been individually written

Anything else is queued and pushed to Discord/Slack/Telegram for approval. The rule lives in
`agent/guards.py`, not in the prompt — the model cannot argue with it.

## Architecture

```
config.py         credentials, env, per-account policy
logging_setup.py  redaction at the formatter, so a careless call site cannot leak
storage/db.py     SQLite WAL, cursors, approvals, audit log
providers/        base.py interface; gmail.py; graph.py
brain/style.py    voice seeding + the continuous learn loop
agent/guards.py   the send gate and injection filter  ← security boundary
agent/client.py   Anthropic SDK against router.bynara.id
agent/tools.py    tool schemas + executors (guards re-checked at execution)
agent/runner.py   the loop, prompt caching, token cap
agent/brief.py    morning brief, quiet threads, skills
notify/channels.py Discord (buttons), Telegram, Slack
```

## Cost control

1. Prompt caching on the frozen prefix — the style profile and tool schemas are byte-stable.
   Volatile content (timestamps, new mail) lives in the user turn, never in the system prompt.
2. One call per batch of mail, not one per message.
3. UID cursor plus `processed_at` — a message is never processed twice.
4. Daily token cap enforced before the call, not after.

Check `usage.cache_read_input_tokens` in the `runs` table. If it stays at zero, a volatile value
has crept into the cached prefix.

## Security

- Email bodies are fenced as untrusted data before the model sees them.
- Injection patterns are checked inbound and outbound.
- The send gate is code, not prompt.
- No credential or token is ever placed in a message.
- systemd unit uses `ProtectSystem=strict` with narrow `ReadWritePaths`.

Threat model: an agent that reads attacker-controlled email and can send mail is an exfiltration
channel. The defences above are what keep a hostile email from becoming a send primitive.

## Tests

```bash
pytest tests/ -v
```

40 tests, no live mailbox. Security tests cover injection detection, every send-gate branch,
approval single-use, and idempotency.
