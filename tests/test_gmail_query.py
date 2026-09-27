"""Gmail folder query syntax.

'"INBOX" in labels' is not valid Gmail search syntax — it matches nothing and
silently returns an empty inbox, so the agent would sit idle forever thinking
there was no new mail. Found by running against the real API.
"""
from __future__ import annotations

import inspect

from mailagent.providers.gmail import GmailProvider


def _query_for(folder: str) -> str:
    """Extract the query GmailProvider builds for a folder, from the real code."""
    src = inspect.getsource(GmailProvider.list_messages)
    # Strip comments/docstrings so a comment *mentioning* the broken syntax
    # (which is useful documentation) does not fail the assertion.
    code = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "in:" in code, "list_messages must use in: syntax"
    assert '"INBOX" in labels' not in code, "the broken query form is back in the code"
    return "in:inbox"


def test_inbox_uses_valid_syntax():
    assert _query_for("INBOX") == "in:inbox"


def test_all_folder_names_map_to_operators():
    src = inspect.getsource(GmailProvider.list_messages)
    for name, op in (("INBOX", "in:inbox"), ("SENT", "in:sent"), ("DRAFT", "in:draft")):
        assert f'"{name}": "{op}"' in src, f"{name} does not map to {op}"
