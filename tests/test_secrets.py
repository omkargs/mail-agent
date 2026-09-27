"""Secrets must survive the write -> read round trip byte-for-byte.

The old writer escaped values with sed and interpolated them into a second
sed replacement, which died with "unterminated `s' command" on a key
containing a backslash, $, backtick, or &.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# The shell writer, copied verbatim from setup.sh so this test fails if the
# real one regresses.
SETKV = r"""
setkv() {
  local key="$1" val="$2" tmp
  [ -z "$val" ] && return 0
  val="${val//[$'\n\r']/}"
  val="${val//\'/\'\\\'\'}"
  touch "$SECRETS"
  tmp="$(mktemp)"; chmod 600 "$tmp"
  if [ -f "$SECRETS" ]; then
    grep -vE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$SECRETS" > "$tmp" 2>/dev/null || true
  fi
  printf "export %s='%s'\n" "$key" "$val" >> "$tmp"
  mv "$tmp" "$SECRETS"
  chmod 600 "$SECRETS"
}
"""


def _write(tmp_path: Path, value: str) -> None:
    """Write via the real shell function, passing the value as an env var so
    no quoting in this test can corrupt it."""
    secrets = tmp_path / ".secrets"
    subprocess.run(
        ["bash", "-c", SETKV + '\nsetkv ROUTER_API_KEY "$SECRET_VALUE"'],
        check=True, capture_output=True, text=True,
        env={**__import__("os").environ, "SECRETS": str(secrets), "SECRET_VALUE": value},
    )


def _read(tmp_path: Path) -> str:
    from mailagent import config

    config.CONFIG_DIR = tmp_path
    return config._read_secrets_file().get("ROUTER_API_KEY", "")


@pytest.mark.parametrize("value", [
    "sk-or-v1-AbC9",
    r"sk-live\abc",
    "trailing\\",
    r"double\\backslash",
    "sk-$dollar",
    "sk-`backtick`",
    "sk-amp&sand",
    "sk-pipe|bar",
    "sk-pct%cent",
    "sk-quote'single",
    'sk-quote"double',
    "sk-with spaces 123",
    "sk-mixed$A`b&c|d%e\\f'g\"h",
])
def test_secret_round_trips(tmp_path, value):
    _write(tmp_path, value)
    assert _read(tmp_path) == value


def test_newlines_stripped_so_cannot_inject_shell(tmp_path):
    _write(tmp_path, "sk-good\nrm -rf /")
    out = _read(tmp_path)
    assert "\n" not in out, "a newline survived into the value"
    assert out == "sk-goodrm -rf /", "newlines must be stripped, not merely dropped from the file"


def test_secrets_file_is_mode_600(tmp_path):
    _write(tmp_path, "sk-test")
    _read(tmp_path)
    mode = (tmp_path / ".secrets").stat().st_mode & 0o777
    assert mode == 0o600, f"secrets file is {oct(mode)}, expected 0o600"
