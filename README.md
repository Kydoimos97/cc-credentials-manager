# cc-cred

Claude Code credential manager and autonomous runner. Manages multiple OAuth tokens
across subscriptions, auto-rotates on rate limit, and tracks session usage.

## Install

```bash
uv tool install /path/to/cc-cred
# or from git:
uv tool install git+https://github.com/YOUR_USER/cc-cred.git
```

## Setup (new machine)

**1. Generate a token for each Claude subscription:**

```bash
claude auth login        # authenticate
claude setup-token       # prints a sk-ant-oat... token valid for 1 year
```

**2. Register it:**

```bash
cc-creds add <token> --label "willem@wrench.ai"
```

Repeating for each account. The first credential added becomes active automatically.

**3. Install session tracking hooks:**

```bash
cc-creds install-hook
```

Registers `Stop`, `StopFailure`, and `UserPromptSubmit` hooks in
`~/.claude/settings.json`. Creates the file if it doesn't exist.

**4. Done.** Start a new terminal so the token is in your environment, then use
`claude` normally. All sessions are tracked in `~/.cc-creds/sessions.jsonl`.

## Commands

```
cc-creds                        open the interactive TUI
cc-creds add <token>            register a new credential
cc-creds list                   list all credentials with live status
cc-creds status                 show active credential (re-verifies)
cc-creds status --label         print just the label, no API call (safe for scripts/hooks)
cc-creds set-active <label>     switch active credential
cc-creds rotate                 advance to next available credential
cc-creds install-hook           register session tracking hooks
```

## Autonomous runner

```bash
claude-auto "do something"             positional prompt
claude-auto -p "do something"          flag prompt
claude-auto -f prompt.txt              read prompt from file
claude-auto --cwd /path/to/project     set working directory
claude-auto --resume <session-id>      resume a specific session
```

Automatically selects the active credential, rotates on rate limit, and resumes
the session with the new token.

## Testing rotation

```bash
CC_CREDS_FORCE_LIMIT_1=1 claude-auto "hello"          skip cred 1, use cred 2
CC_CREDS_FORCE_LIMIT_1=1 CC_CREDS_FORCE_LIMIT_2=1 claude-auto "hello"   exhaust all
```

Force-limit is in-memory only — credentials are not marked limited in the store.

## Debug

```bash
CC_CREDS_DEBUG=1 cc-creds status
CC_CREDS_DEBUG=1 claude-auto "hello"
```
