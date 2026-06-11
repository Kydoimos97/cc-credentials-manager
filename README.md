# Credentials Manager for Claude Code

[![CI](https://github.com/Kydoimos97/cc-credentials-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/Kydoimos97/cc-credentials-manager/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cc-credentials-manager)](https://pypi.org/project/cc-credentials-manager/)
[![Python](https://img.shields.io/pypi/pyversions/cc-credentials-manager)](https://pypi.org/project/cc-credentials-manager/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Manages multiple Claude Code OAuth tokens across subscriptions, auto-rotates on
rate limit without losing session context, and tracks per-credential usage via
hooks. Includes an interactive TUI and a drop-in autonomous runner (`claude-auto`).

## Prerequisites

- **Python 3.11+**
- **uv** — https://docs.astral.sh/uv/getting-started/installation/
- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code` (the `claude` binary must be on PATH — the Agent SDK calls it as a subprocess)

## Install

```bash
uv tool install cc-credentials-manager
```

Or directly from GitHub:

```bash
uv tool install git+https://github.com/Kydoimos97/cc-credentials-manager.git
```

This puts two binaries on PATH: `cc-creds` and `claude-auto`.

## Setup

**Step 1 — generate a long-lived token for each Claude subscription:**

```bash
claude auth login       # authenticate with your Claude account
claude setup-token      # prints a sk-ant-oat... token valid for 1 year
```

**Step 2 — register it:**

```bash
cc-creds add <token> --label "account-name"
```

The token is verified against the API immediately. The first credential added
becomes active automatically.

**Step 3 — install session tracking hooks:**

```bash
cc-creds install-hook
```

Registers `Stop`, `StopFailure`, and `UserPromptSubmit` hooks in
`~/.claude/settings.json`. Creates the file if it does not exist yet.

That's it. Use `claude` normally — all sessions are tracked automatically.

The active credential is stored on disk in `~/.cc-creds/`. `claude-auto` reads it
at startup and passes the token explicitly to each run — no global environment
injection, so interactive `claude` sessions are never affected.

## Commands

### Credential management

```
cc-creds                              open the interactive TUI (Credentials, Stats, Usage tabs)
cc-creds add <token> [--label NAME]   register and verify a new credential
cc-creds list                         list all credentials with live API status
cc-creds status                       show auth mode and active label (no API call)
cc-creds status --verify              re-verify token against API, show full status
cc-creds status --json                machine-readable JSON output for scripts/statusline
cc-creds status --label               print just the active label — safe for hooks/scripts
cc-creds set-active <label-or-id>     switch active credential
cc-creds remove <label-or-id>         remove a credential (scrubs token from registry and settings.json if active)
cc-creds deactivate                   clear active credential, return to full OAuth session
cc-creds rotate                       advance to next available credential
cc-creds install-hook                 register session tracking hooks in ~/.claude/settings.json
```

**TUI keybindings (Credentials tab):**

```
a       add new credential (opens token + label form)
d       delete selected credential
enter   set selected as active
r       rotate to next available credential
i       install session tracking hooks
v       verify selected credential against the API
q       quit
```

### Autonomous runner

```
claude-auto "prompt"                  run a prompt non-interactively
claude-auto -p "prompt"               same, -p flag for muscle memory
claude-auto -f prompt.txt             read prompt from file
claude-auto --cwd /path/to/project    set working directory (default: cwd)
claude-auto --resume <session-id>     resume a specific session
```

Automatically picks the active credential, rotates on rate limit, and resumes
the same session with the new token.

## How it works

### Credential store — `~/.cc-creds/`

```
credentials.json    registered tokens with labels and expiry
cred-status.json    per-credential status: available / limited / admin_disabled
active.key          currently active credential ID (absent when deactivated)
sessions.jsonl      session registry with cost and token data
env                 shell env file written on non-Windows (source from .bashrc/.zshrc)
```

### Auth modes

cc-creds supports two modes which can be switched at any time without restarting
your session:

**OAuth2-Full (default)** — `claude` uses your real OAuth session with full scope.
Remote Control, billing, and all Claude Code features work normally. This is the
default when no credential is active.

**LLT-Limited** — a long-lived token is active. Claude Code uses that token for
API calls, which means usage comes from the associated subscription quota. Some
features that require the full OAuth session (e.g. Remote Control) are unavailable.

Switch modes:
```bash
cc-creds set-active <label>   # activate an LLT — persists to user env vars
cc-creds deactivate           # return to OAuth2-Full — clears user env vars
```

`set-active` writes the token to the Windows user environment (`HKCU\Environment`)
so new terminals and Claude Code sessions inherit it. `deactivate` removes it.
Neither touches `settings.json` — that would break interactive sessions.

`claude-auto` always reads the active credential from disk and passes the token
explicitly per run, regardless of what is in the shell environment.

### Session tracking

Hooks registered by `install-hook` fire on every session:
- `UserPromptSubmit` — registers the session, records rolling cost/token data
- `Stop` — finalises status to `success`, records final cost/tokens
- `StopFailure` — detects rate limits, rotates to next credential automatically

Cost and token data comes from `$CCODE_HOME/states/sessions/` when the `CCODE_HOME`
environment variable is set and points to your Claude Code data directory. Without
it, the `Stop` hook falls back to parsing the Claude transcript JSONL directly —
this path is less accurate and does not report rolling cost during active sessions
(`UserPromptSubmit`). Set `CCODE_HOME` for reliable tracking.

### Rate limit rotation

When a rate limit is hit mid-session, `claude-auto`:
1. Catches the `RateLimitEvent` or `ProcessError` from the SDK
2. Marks the current credential as limited with the reset time when available (parsed from session state or assistant text; may be absent if not reported)
3. Rotates to the next available credential
4. Resumes the same session with `ClaudeAgentOptions(resume=session_id)`

Interactive `claude` sessions are handled by the `StopFailure` hook — Claude
rotates the active credential and the next session starts with the fresh token.

## Testing rotation

```bash
# Force-skip credential 1 (in-memory only, no store write):
CC_CREDS_FORCE_LIMIT_1=1 claude-auto "hello"

# Exhaust all credentials:
CC_CREDS_FORCE_LIMIT_1=1 CC_CREDS_FORCE_LIMIT_2=1 claude-auto "hello"
```

## Debug logging

```bash
CC_CREDS_DEBUG=1 cc-creds status
CC_CREDS_DEBUG=1 cc-creds list
CC_CREDS_DEBUG=1 claude-auto "hello"
```

Outputs structured Rich logs to stderr: API calls with full response headers,
SDK message stream, rotation decisions, session registration.

## Statusline integration

If you use a Claude Code statusline script, `cc-creds status --json` returns:
```json
{"mode": "LLT-Limited", "label": "account-name"}
{"mode": "OAuth2-Full", "label": null}
```
Use this to display the current auth mode in your statusline.

## Troubleshooting

**Hooks not firing or showing errors**

Check that `cc-creds install-hook` ran successfully and that `~/.claude/settings.json`
contains entries under `hooks.Stop`, `hooks.StopFailure`, and `hooks.UserPromptSubmit`.
Hook payloads are JSON objects sent via stdin — the `hook-event` handler expects
`session_id`, `type`, and `cwd` fields. If Claude Code changes its hook payload
schema, the handler will silently skip unrecognised payloads.

**`cc-creds list` shows "unknown" status after a network error**

Status checks that fail due to network errors (timeout, DNS failure) are retried
after 5 minutes. A credential in "unknown" state is treated as available for
rotation — it will be re-verified the next time rotation tries to use it.

**Credential removed but old token still active in a new terminal**

`cc-creds remove` scrubs the token from `HKCU\Environment` and `settings.json`
when the removed credential was active. Open a new terminal after removing an
active credential to pick up the cleared environment.
