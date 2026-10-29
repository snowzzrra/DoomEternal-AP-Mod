import sys
from pathlib import Path

import pytest

from tools.diagnostics import save_inspector


@pytest.mark.parametrize(("exists", "message"), [
    (False, "folder does not exist"),
    (True, "no save found"),
])
def test_cli_distinguishes_missing_and_empty_folders(monkeypatch, capsys, exists, message):
    monkeypatch.setattr(sys, "argv", ["save_inspector", "synthetic-saves"])
    monkeypatch.setattr(Path, "is_dir", lambda path: exists)
    monkeypatch.setattr(Path, "glob", lambda path, pattern: iter(()))
    with pytest.raises(SystemExit) as error:
        save_inspector.main()
    assert error.value.code == 2
    assert message in capsys.readouterr().err
