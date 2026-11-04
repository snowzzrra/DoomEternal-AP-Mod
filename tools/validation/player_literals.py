"""Reject uncatalogued launcher display text; no runtime dependency or baseline."""

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIDGETS = {
    "QLabel",
    "QPushButton",
    "QCheckBox",
    "QRadioButton",
    "QToolButton",
    "QTableWidgetItem",
    "QLineEdit",
    "QAction",
    "_OptionCategory",
    "_ScanlineButton",
}
SETTERS = {
    "setText",
    "setWindowTitle",
    "setPlaceholderText",
    "setToolTip",
    "setStatusTip",
    "setAccessibleName",
    "setAccessibleDescription",
    "setPlainText",
    "setHtml",
    "setInformativeText",
    "setDetailedText",
    "setHeaderLabels",
    "setHorizontalHeaderLabels",
    "setLabelText",
    "setComboBoxItems",
    "showMessage",
}
FIELDS = {"message", "user_message", "user_title", "user_action", "summary_message", "notification"}
POSITIONS = {
    "addTab": (1,),
    "Diagnostic": (2,),
    "ConnectionFailure": (1, 2, 3),
    "RepairAction": (1, 2),
    "_set_home": (0, 1, 2, 3),
    "_set_setup_state": (1,),
    "_set_connection_badge": (0,),
}
# these exact values carry layout or numeric data, not player prose
ALLOWED = {
    "": "empty widget",
    " ": "spacing",
    "\n": "line separator",
    "\n\n": "paragraph separator",
    ": ": "field separator",
    ", ": "list separator",
    " | ": "field separator",
    " / ": "count separator",
    "0": "initial count",
    "1": "initial quantity",
    "00": "timer padding",
    "02d": "timer format",
}
ALLOWED.update(
    {
        " · ": "field separator",
        "• ": "list marker",
        "<br>": "HTML line break",
        "<b>": "HTML emphasis",
        "</b> &nbsp; ": "HTML emphasis and spacing",
    }
)
SHELL_LOGS = {'echo "Starting AP Client: ${NEW_CMD[@]}" >> "$DIR/bridge_debug.log"': "technical bridge command log"}


def name(node):
    return ast.unparse(node)


def display_arguments(node):
    target = name(node.func)
    short = target.rsplit(".", 1)[-1]
    indices = ()
    if (
        short in WIDGETS
        or short in SETTERS
        or short
        in {"_label", "addItem", "addItems", "insertPlainText", "appendPlainText", "notify", "send_notification"}
    ):
        indices = (0,)
    elif target.startswith(("QMessageBox.", "QInputDialog.")):
        indices = (1, 2)
    elif target.startswith("QFileDialog."):
        indices = (1, 3)
    else:
        indices = POSITIONS.get(short, ())
    return [node.args[i] for i in indices if i < len(node.args)] + [
        kw.value for kw in node.keywords if kw.arg in FIELDS
    ]


def bind(target, value, bindings):
    if isinstance(target, ast.Name | ast.Attribute):
        bindings.setdefault(name(target), []).append(value)
    elif isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Tuple | ast.List):
        for left, right in zip(target.elts, value.elts, strict=False):
            bind(left, right, bindings)


def scope_nodes(scope):
    stack = list(scope.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def bindings_for(nodes):
    bindings = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value, bindings)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            bind(node.target, node.value, bindings)
    for node in nodes:
        if isinstance(node, ast.For):
            bind_loop(node, bindings)
    return bindings


def bind_loop(node, bindings):
    for sequence in loop_sequences(node.iter, bindings):
        for value in sequence.elts:
            bind(node.target, value, bindings)


def loop_sequences(node, bindings):
    if isinstance(node, ast.Tuple | ast.List):
        return (node,)
    if isinstance(node, ast.Call) and name(node.func) == "enumerate" and node.args:
        return tuple(
            ast.Tuple(elts=[ast.Tuple(elts=[ast.Constant(index), value]) for index, value in enumerate(sequence.elts)])
            for sequence in loop_sequences(node.args[0], bindings)
        )
    return tuple(value for value in bindings.get(name(node), ()) if isinstance(value, ast.Tuple | ast.List))


def literals(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    elif isinstance(node, ast.Name | ast.Attribute):
        yield from reference_literals(node, bindings, seen)
    elif isinstance(node, ast.Call):
        yield from call_literals(node, bindings, seen)
    else:
        for child in text_children(node):
            yield from literals(child, bindings, seen)


def reference_literals(node, bindings, seen):
    key = name(node)
    if key not in seen:
        for value in bindings.get(key, ()):
            yield from literals(value, bindings, seen | {key})


def call_literals(node, bindings, seen):
    target = name(node.func).rsplit(".", 1)[-1]
    if target == "get":
        for fallback in node.args[1:]:
            yield from literals(fallback, bindings, seen)
        return
    if target not in {"str", "join", "format", "upper", "lower", "strip"}:
        return
    for arg in node.args:
        yield from literals(arg, bindings, seen)
    if isinstance(node.func, ast.Attribute):
        yield from literals(node.func.value, bindings, seen)


def text_children(node):
    if isinstance(node, ast.Dict):
        return node.values
    if isinstance(node, ast.IfExp):
        return (node.body, node.orelse)
    if isinstance(node, ast.Subscript | ast.Compare | ast.BoolOp):
        return ()
    if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        return (node.elt,)
    return ast.iter_child_nodes(node)


def issues(source, filename):
    tree = ast.parse(source, filename=filename)
    found = {}
    scopes = [tree] + [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
    globals_ = bindings_for(list(scope_nodes(tree)))
    for scope in scopes:
        nodes = list(scope_nodes(scope))
        bindings = globals_ | bindings_for(nodes)
        for node in nodes:
            for value in sink_values(node):
                for literal in literals(value, bindings):
                    if literal.value not in ALLOWED:
                        found[(literal.lineno, literal.col_offset)] = literal.value
    return [f"{filename}:{line}:{col + 1}: player literal {value!r}" for (line, col), value in sorted(found.items())]


def sink_values(node):
    if isinstance(node, ast.Call):
        return display_arguments(node)
    if isinstance(node, ast.Dict):
        return [
            value
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and key.value in FIELDS
        ]
    return []


def scan(root=ROOT):
    failures = [
        issue
        for path in sorted((root / "doom_eap").rglob("*.py"))
        for issue in issues(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
    ]
    script = root / "scripts/launch/run_bridge.sh"
    if script.is_file():
        failures.extend(shell_issues(script.read_text(encoding="utf-8"), script.relative_to(root).as_posix()))
    return failures


def shell_issues(source, filename):
    return [
        f"{filename}:{line}: player shell literal {text.strip()!r}"
        for line, text in enumerate(source.splitlines(), 1)
        if text.strip().startswith(("echo ", "printf ")) and text.strip() not in SHELL_LOGS
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    failures = scan(args.root)
    for failure in failures:
        print(failure)
    print(f"player literals: {len(failures)} violations")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
