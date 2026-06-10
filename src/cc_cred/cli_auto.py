import sys
from pathlib import Path
from typing import Optional

import click

from cc_cred._logging import configure_logging
from cc_cred.runner import run_sync


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("prompt", required=False)
@click.option("-p", "--print", "prompt_flag", default=None, metavar="PROMPT", help="Prompt text.")
@click.option("-f", "--file", "prompt_file", type=click.Path(exists=True), default=None, help="File containing the prompt.")
@click.option("--cwd", "cwd", type=click.Path(), default=None, help="Working directory for the agent session.")
@click.option("--resume", "session_id", default=None, metavar="SESSION_ID", help="Resume a specific session by ID.")
def main(
    prompt: Optional[str],
    prompt_flag: Optional[str],
    prompt_file: Optional[str],
    cwd: Optional[str],
    session_id: Optional[str],
) -> None:
    configure_logging()
    """Run a Claude agent session with automatic credential rotation.

    Prompt sources (first match wins):
      1. Positional PROMPT argument
      2. -p / --print flag
      3. -f / --file path
      4. stdin (when piped)
    """
    prompt_text: Optional[str] = None

    if prompt:
        prompt_text = prompt
    elif prompt_flag:
        prompt_text = prompt_flag
    elif prompt_file:
        prompt_text = Path(prompt_file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        prompt_text = sys.stdin.read()

    if not prompt_text and not session_id:
        click.echo("Error: no prompt provided. Use a positional argument, -p, -f, or pipe via stdin.", err=True)
        sys.exit(1)

    if not prompt_text:
        prompt_text = ""

    exit_code = run_sync(
        prompt_text.strip(),
        cwd=Path(cwd) if cwd else None,
        session_id=session_id,
    )
    sys.exit(exit_code)
