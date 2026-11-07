"""Check Python functions against cognitive 15 and cyclomatic 10."""

from __future__ import annotations

import argparse
import ast
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ScanError(ValueError):
    pass


@dataclass(frozen=True)
class Metrics:
    cognitive: int
    cyclomatic: int
    nesting: int
    lines: int


class Complexity(ast.NodeVisitor):
    def __init__(self):
        self.cognitive = 0
        self.cyclomatic = 1
        self.depth = 0
        self.function_depth = 0
        self.nesting = 0
        self.boolean_nodes = set()

    def decision(self, cognitive=True):
        if cognitive:
            self.cognitive += 1 + self.depth
        if self.function_depth == 0:
            self.cyclomatic += 1

    def block(self, nodes, increment=1):
        self.depth += increment
        self.nesting = max(self.nesting, self.depth)
        for node in nodes:
            self.visit(node)
        self.depth -= increment

    def otherwise(self, nodes):
        if nodes:
            self.cognitive += 1
            self.block(nodes)

    def visit_If(self, node, elif_clause=False):
        self.decision(cognitive=not elif_clause)
        if elif_clause:
            self.cognitive += 1
        self.visit(node.test)
        self.block(node.body)
        if len(node.orelse) == 1 and type(node.orelse[0]) is ast.If:
            child = node.orelse[0]
            if child.col_offset == node.col_offset:
                self.visit_If(child, elif_clause=True)
                return
        self.otherwise(node.orelse)

    def visit_For(self, node):
        self.decision()
        self.visit(node.target)
        self.visit(node.iter)
        self.block(node.body)
        self.otherwise(node.orelse)

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        self.decision()
        self.visit(node.test)
        self.block(node.body)
        self.otherwise(node.orelse)

    def visit_ExceptHandler(self, node):
        self.decision()
        if node.type is not None:
            self.visit(node.type)
        self.block(node.body)

    def visit_Try(self, node):
        self.block(node.body, increment=0)
        self.block(node.handlers, increment=0)
        self.otherwise(node.orelse)
        self.block(node.finalbody, increment=0)

    visit_TryStar = visit_Try

    def visit_IfExp(self, node):
        self.decision()
        self.block((node.test, node.body, node.orelse))

    def boolean_operators(self, node):
        if type(node) is not ast.BoolOp:
            return
        self.boolean_nodes.add(node)
        yield from self.boolean_operators(node.values[0])
        for value in node.values[1:]:
            yield type(node.op)
            yield from self.boolean_operators(value)

    def visit_BoolOp(self, node):
        if node not in self.boolean_nodes:
            previous = None
            for operator in self.boolean_operators(node):
                self.cognitive += operator is not previous
                previous = operator
        if self.function_depth == 0:
            self.cyclomatic += len(node.values) - 1
        self.generic_visit(node)

    def visit_comprehension(self, node):
        if self.function_depth == 0:
            self.cyclomatic += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Assert(self, node):
        self.decision(cognitive=False)
        self.generic_visit(node)

    def visit_Match(self, node):
        self.cognitive += 1 + self.depth
        self.visit(node.subject)
        for case in node.cases:
            self.visit_match_case(case)

    def visit_match_case(self, node):
        wildcard = type(node.pattern) is ast.MatchAs and node.pattern.pattern is None
        if not wildcard:
            self.decision(cognitive=False)
        self.visit(node.pattern)
        if node.guard is not None:
            self.decision(cognitive=False)
            self.visit(node.guard)
        self.block(node.body)

    def visit_FunctionDef(self, node):
        # simple decorator wrappers do not add a nesting penalty in Sonar's Python model
        parent = self.current_function
        increment = 0
        if parent is not None:
            increment = not all(simple_return(child) for child in parent.body if child is not node)
        self.current_function = node
        self.function_depth += 1
        self.block(node.body, increment=increment)
        self.function_depth -= 1
        self.current_function = parent

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        depth = self.depth
        parent = self.current_function
        self.current_function = None
        self.depth = 0
        self.function_depth += 1
        self.block(node.body, increment=0)
        self.function_depth -= 1
        self.depth = depth
        self.current_function = parent


def simple_return(node):
    return type(node) is ast.Return and type(node.value) is ast.Name


def measure(node):
    visitor = Complexity()
    visitor.current_function = node
    visitor.block(node.body, increment=0)
    return Metrics(visitor.cognitive, visitor.cyclomatic, visitor.nesting, node.end_lineno - node.body[0].lineno + 1)


class Functions(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.names = []
        self.functions = {}

    def visit_FunctionDef(self, node):
        self.names.append(node.name)
        key = f"{self.path}::{'.'.join(self.names)}"
        if key in self.functions:
            raise ScanError(f"duplicate function name: {key}")
        self.functions[key] = measure(node)
        self.generic_visit(node)
        self.names.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.names.append(node.name)
        self.generic_visit(node)
        self.names.pop()


def scan(root, directories):
    functions = {}
    for directory in directories:
        if not directory.is_dir():
            raise ScanError(f"source directory does not exist: {directory}")
        for path in sorted(directory.rglob("*.py")):
            with tokenize.open(path) as source:
                contents = source.read()
            tree = ast.parse(contents, filename=str(path))
            visitor = Functions(path.relative_to(root).as_posix())
            visitor.visit(tree)
            functions.update(visitor.functions)
    if not functions:
        raise ScanError(f"no Python functions found under {root}")
    return functions


def failures(functions):
    result = []
    for key, metrics in functions.items():
        if metrics.cognitive > 15:
            result.append(f"{key}: cognitive {metrics.cognitive} > 15")
        if metrics.cyclomatic > 10:
            result.append(f"{key}: cyclomatic {metrics.cyclomatic} > 10")
    return result


def warnings(functions):
    result = []
    for key, metrics in functions.items():
        reasons = []
        if metrics.nesting > 4:
            reasons.append(f"nesting {metrics.nesting}")
        if metrics.lines > 150:
            reasons.append(f"body {metrics.lines} lines")
        if reasons:
            result.append(f"{key}: {', '.join(reasons)}")
    return result


def run_gate(root, directories, *, verbose=False):
    functions = scan(root, directories)
    errors = failures(functions)
    if verbose:
        for message in warnings(functions):
            print(f"WARNING {message}")
    for message in errors:
        print(f"ERROR {message}")
    if errors:
        print(f"FAIL {root.name}: {len(errors)} failures, {len(functions)} functions")
        return 1
    print(f"PASS {root.name}: {len(functions)} functions, cognitive <= 15, cyclomatic <= 10")
    return 0


def arguments(argv):
    parser = argparse.ArgumentParser(
        description="Development-only Python function complexity gate (stdlib AST).",
        epilog=(
            "Cognitive scoring follows Sonar's Python model: nested flow, elif/else, "
            "except, ternaries and boolean operator runs; try/finally, with, comprehensions "
            "and unlabeled jumps add no cognitive points. Nested function bodies count "
            "toward enclosing cognitive scores, with the simple-wrapper exception; "
            "each named function also gets its own score. Match counts as one switch. "
            "Cyclomatic scoring starts at 1 and counts if/elif, loops, except, ternaries, "
            "boolean operators, comprehension generators/filters, assert, match choices "
            "and guards; nested definitions are separate. Nesting measures flow depth; "
            "body length spans the first through last body line, including blanks/comments. "
            "All .py files in each scope, including tests and generated files, are scanned. "
            "Keys use relative POSIX paths and dotted class/function names; duplicate "
            "qualified names are errors. All functions must have cognitive <= 15 and "
            "cyclomatic <= 10."
        ),
    )
    parser.add_argument("--apworld-root", type=Path, help="check only this APWorld directory; default: Mod doom_eap/tools/scripts/tests")
    parser.add_argument("--verbose", action="store_true", help="print complexity, nesting and body length warnings")
    return parser.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    root = ROOT
    directories = [root / name for name in ("doom_eap", "tools", "scripts", "tests")]
    if args.apworld_root is not None:
        root = args.apworld_root.resolve()
        directories = [root]
    try:
        return run_gate(root, directories, verbose=args.verbose)
    except (OSError, SyntaxError, UnicodeError, ValueError) as error:
        print(f"ERROR {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
