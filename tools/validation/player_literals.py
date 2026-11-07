"""Reject uncatalogued launcher display text; no runtime dependency or baseline."""

import argparse
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GAME_TEXT_FUNCTIONS = {
    "notification_text", "major_notification_text", "location_sent_text", "item_receipt_text",
    "build_string_table", "string_entries", "_apply_placement_strings",
}
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
        indices = (1,)
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


def issues(source, filename, *, game_only=False):
    tree = ast.parse(source, filename=filename)
    found = {}
    scopes = text_scopes(tree, game_only)
    globals_ = bindings_for(list(scope_nodes(tree)))
    for scope in scopes:
        nodes = list(scope_nodes(scope))
        bindings = globals_ | bindings_for(nodes)
        for node in nodes:
            values = sink_values(node, game_only=game_only)
            for literal in (literal for value in values for literal in literals(value, bindings)):
                if not technical_literal(literal.value):
                    found[(literal.lineno, literal.col_offset)] = literal.value
    return [f"{filename}:{line}:{col + 1}: player literal {value!r}" for (line, col), value in sorted(found.items())]


def text_scopes(tree, game_only):
    scopes = [tree] + [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
    if game_only:
        scopes = [scope for scope in scopes if getattr(scope, "name", "") in GAME_TEXT_FUNCTIONS]
    return scopes


def technical_literal(value):
    if re.fullmatch(r"(?:[\w./\\:-]+\.(?:exe|dll|bat|cmd|ps1|sh|json|jsonl|log|cfg|txt|state|zip)|--[a-z][a-z0-9-]*|[a-zA-Z][a-zA-Z0-9]*_[a-zA-Z0-9_]+)", value):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\r\n]+", value):
        return True
    if value in ALLOWED or value in {"Player", "archipelago.gg:38281"}:
        return True
    return not re.search(r"[A-Za-zÀ-ÿ]", re.sub(r"\{[a-z0-9_]+\}", "", value))


def sink_values(node, *, game_only=False):
    if game_only:
        return game_text_values(node)
    if isinstance(node, ast.Call):
        return display_arguments(node)
    if isinstance(node, ast.Dict):
        return [
            value
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and key.value in FIELDS
        ]
    return []


def game_text_values(node):
    match node:
        case ast.Return(value=value):
            return [value] if value is not None else []
        case ast.Call(func=ast.Attribute(value=ast.Name(id="entries"), attr="append"),
                      args=[ast.Tuple(elts=[_, value])]):
            return [value]
        case ast.Assign(targets=[ast.Subscript(slice=ast.Constant(value="text"))], value=value):
            return [value]
        case ast.Dict(keys=keys, values=values):
            return [value for key, value in zip(keys, values, strict=True)
                    if isinstance(key, ast.Constant) and key.value == "text"]
        case _:
            return []


def scan(root=ROOT):
    failures = [
        issue
        for directory in ("doom_eap/launcher/ui", "doom_eap/launcher/presenters")
        for path in sorted((root / directory).rglob("*.py"))
        for issue in issues(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
    ]
    for path in sorted((root / "doom_eap").rglob("*.py")):
        failures.extend(player_error_issues(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix()))
    for filename in ("tools/maps/notification_formatting.py", "tools/release/build_string_table.py",
                     "doom_eap/launcher/launcher_core.py"):
        path = root / filename
        failures.extend(issues(path.read_text(encoding="utf-8"), filename, game_only=True))
    return failures


def player_error_issues(source, filename):
    return [
        f"{filename}:{node.lineno}: PlayerError requires catalog category and key"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and name(node.func) == "PlayerError"
        and (len(node.args) != 2 or not all(isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                                          for arg in node.args))
    ]


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
