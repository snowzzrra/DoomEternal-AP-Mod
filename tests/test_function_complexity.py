"""Protect complexity gate behavior with small Python sources."""

import pytest

from tools.validation.function_complexity import ExceptionsError, Mode, run_gate


def source_function(name="f", branches=0, nested=False):
    if nested:
        body = "".join("    " * level + "if value:\n" for level in range(1, 7))
        return f"def {name}():\n" + body + "    " * 7 + "pass\n"
    return f"def {name}():\n" + "    if value:\n        pass\n" * branches + "    pass\n"


@pytest.fixture
def gate(tmp_path):
    source = tmp_path / "sample.py"
    exceptions = tmp_path / "exceptions.txt"
    exceptions.write_text("# path::qualified.name cognitive cyclomatic\n", encoding="utf-8")
    return tmp_path, source, exceptions


@pytest.mark.parametrize("source", [source_function(branches=10), source_function(nested=True)])
def test_unlisted_function_over_either_limit_fails(gate, source, capsys):
    root, path, exceptions = gate
    path.write_text(source, encoding="utf-8")
    assert run_gate(root, [root], exceptions) == 1
    assert "ERROR sample.py::f:" in capsys.readouterr().out
    path.write_text(source_function(branches=9), encoding="utf-8")
    assert run_gate(root, [root], exceptions) == 0


@pytest.mark.parametrize(("recorded", "source"), [
    ("20 8", source_function(nested=True)),
    ("12 11", source_function(branches=11)),
])
def test_listed_function_cannot_raise_either_score(gate, recorded, source):
    root, path, exceptions = gate
    path.write_text(source, encoding="utf-8")
    exceptions.write_text(f"sample.py::f {recorded}\n", encoding="utf-8")
    assert run_gate(root, [root], exceptions) == 1


@pytest.mark.parametrize("recorded", ["16 17", "21 7"])
def test_improved_function_requires_removal_and_update_removes_it(gate, capsys, recorded):
    root, path, exceptions = gate
    path.write_text(source_function(branches=9), encoding="utf-8")
    exceptions.write_text(f"sample.py::f {recorded}\n", encoding="utf-8")
    assert run_gate(root, [root], exceptions) == 1
    assert "remove its exception line or run --update" in capsys.readouterr().out
    assert run_gate(root, [root], exceptions, Mode.UPDATE) == 0
    assert "sample.py::f" not in exceptions.read_text(encoding="utf-8")
    assert run_gate(root, [root], exceptions) == 0


def test_update_lowers_scores_and_drops_missing_functions_without_adding(gate):
    root, path, exceptions = gate
    path.write_text(source_function(branches=10) + source_function("added"), encoding="utf-8")
    exceptions.write_text("sample.py::f 20 21\nsample.py::gone 20 21\n", encoding="utf-8")
    assert run_gate(root, [root], exceptions, Mode.UPDATE) == 0
    assert exceptions.read_text(encoding="utf-8").splitlines()[1:] == ["sample.py::f 10 11"]


@pytest.mark.parametrize(("entries", "source"), [
    ("sample.py::f 10 11\n", source_function(branches=11)),
    ("", source_function(branches=10)),
])
def test_update_rejects_increases_and_unlisted_complex_functions(gate, entries, source):
    root, path, exceptions = gate
    path.write_text(source, encoding="utf-8")
    exceptions.write_text(entries, encoding="utf-8")
    assert run_gate(root, [root], exceptions, Mode.UPDATE) == 1
    assert exceptions.read_text(encoding="utf-8") == entries


@pytest.mark.parametrize("line", [
    "bad-key 16 11",
    "sample.py:: 16 11",
    "sample.py::f many 11",
    "sample.py::f -1 11",
    "sample.py::f 16 0",
    "sample.py::f 16",
    "sample.py::f 16 11 extra",
    "sample.py::f 16 11\nsample.py::f 16 11",
])
def test_invalid_exception_line_is_rejected(gate, line):
    root, path, exceptions = gate
    path.write_text(source_function(), encoding="utf-8")
    exceptions.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ExceptionsError, match="invalid exception line"):
        run_gate(root, [root], exceptions)
