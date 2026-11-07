import pytest

from tools.validation.function_complexity import run_gate


def source_function(name="f", branches=0, nested=False):
    if nested:
        body = "".join("    " * level + "if value:\n" for level in range(1, 7))
        return f"def {name}():\n" + body + "    " * 7 + "pass\n"
    return f"def {name}():\n" + "    if value:\n        pass\n" * branches + "    pass\n"


@pytest.fixture
def gate(tmp_path):
    source = tmp_path / "sample.py"
    return tmp_path, source


@pytest.mark.parametrize("source", [source_function(branches=10), source_function(nested=True)])
def test_unlisted_function_over_either_limit_fails(gate, source, capsys):
    root, path = gate
    path.write_text(source, encoding="utf-8")
    assert run_gate(root, [root]) == 1
    assert "ERROR sample.py::f:" in capsys.readouterr().out
    path.write_text(source_function(branches=9), encoding="utf-8")
    assert run_gate(root, [root]) == 0
