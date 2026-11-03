"""Every string the package builds, read off its source.

Two guards walk the whole of `comfy_qa/` for the shape of a line it prints —
a gcloud command that stops to ask, a command with a `#` note on the end of
it — and both had the same first draft: a regex over two files somebody named.
That is a check of the files somebody thought of. The command that slipped
past one of them (`gcloud.py`'s `reset-windows-password`, without `--quiet`)
was in a third file, and it was split across two adjacent literals, so a
regex for the whole command could not have seen it even there.

So: the syntax tree of every module, where adjacent literals are already one
node, and every string in it that is not a docstring. An f-string's holes
become `{}`, so `f"comfy-qat down {name}"` reads `comfy-qat down {}` — a shape
to match, not a value.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "comfy_qa"


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of the nodes that are docstrings: prose about the code, not
    something the code says."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.add(id(first.value))
    return found


def strings(package: Path = PACKAGE) -> list[tuple[str, str]]:
    """`(where, text)` for every string literal and f-string in the package.

    `where` is `module.py:line`. The literal pieces INSIDE an f-string are not
    returned on their own — they are already in the f-string's text, and
    returning them twice would let half a command pass for a whole one.
    """
    out: list[tuple[str, str]] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                skip.update(id(part) for part in node.values)
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(part.value if isinstance(part, ast.Constant) else "{}"
                               for part in node.values)
            else:
                continue
            out.append((f"{path.name}:{node.lineno}", text))
    return out


# How many texts one expression may stand for before the reading stops counting.
# Every branch of an `if` expression is a text of its own, so a string built from
# several of them multiplies; none in the package comes near this.
_MOST_VARIANTS = 32


def composed(package: Path = PACKAGE) -> list[tuple[str, str]]:
    """`(where, text)` for every string a function BUILDS out of parts: a local
    name assigned a string once, put into an f-string or joined on with `+`, is
    read as the text it holds, and each branch of an `if` expression as a text
    of its own.

    `strings` reads each literal by itself, and a note kept in a variable is a
    `{}` there. `setup` built its remedy as
    `f"To ask later: comfy-qat quota request --gpu {missing}{where}{no_good}"`,
    with `no_good = f" — note that {region} does not offer …"` three lines up,
    so the dash after the command was in neither literal, and both guards over
    the source passed it. The printed reading passed it too, for a reason of its
    own: setup's tests recorded what it said instead of printing it.

    NARROW ON PURPOSE. One function's own names, not a nested function's; and
    only a name assigned exactly once, by a plain `=`, from string parts. A name
    assigned in two branches would be read with the other branch's value, which
    is a combination the code never makes, and a guard that reports those is one
    people learn to skip. Only texts that an expansion or a `+` changed are
    returned; every other string is `strings`'.
    """
    out: list[tuple[str, str]] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [node for node in ast.walk(tree) if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in scopes:
            nodes = list(_own(scope))
            names = _assigned_once(nodes)
            for node in nodes:
                if not isinstance(node, (ast.JoinedStr, ast.BinOp, ast.IfExp)):
                    continue
                texts = _texts(node, names)
                if texts is None or texts == _texts(node, {}) and not isinstance(
                        node, (ast.BinOp, ast.IfExp)):
                    continue
                out += [(f"{path.name}:{node.lineno}", text) for text in texts]
    return out


def _own(scope: ast.AST):
    """The nodes of `scope` itself, not of a function or class defined in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _assigned_once(nodes: list[ast.AST]) -> dict[str, list[str]]:
    """The names bound exactly once among `nodes`, by `name = <string parts>`,
    and the texts each can hold."""
    bindings: dict[str, list[ast.AST]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For,
                               ast.comprehension, ast.NamedExpr)):
            targets = [node.target]
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            targets = [node.optional_vars]
        else:
            continue
        for target in targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    bindings.setdefault(name.id, []).append(node)
    found = {}
    for name, sites in bindings.items():
        if len(sites) == 1 and isinstance(sites[0], ast.Assign):
            texts = _texts(sites[0].value, {})
            if texts is not None:
                found[name] = texts
    return found


def _texts(node: ast.AST, names: dict[str, list[str]]) -> list[str] | None:
    """Every text `node` can stand for, with `names` read as what they hold and
    any other value as `{}` — or None when `node` is not built of strings."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.JoinedStr):
        parts = [[part.value] if isinstance(part, ast.Constant)
                 else names.get(getattr(part.value, "id", None), ["{}"])
                 for part in node.values]
        return _joined(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _texts(node.left, names), _texts(node.right, names)
        if left is None and right is None:
            return None
        return _joined([left or ["{}"], right or ["{}"]])
    if isinstance(node, ast.IfExp):
        body, orelse = _texts(node.body, names), _texts(node.orelse, names)
        if body is None and orelse is None:
            return None
        return ((body or ["{}"]) + (orelse or ["{}"]))[:_MOST_VARIANTS]
    return None


def _joined(parts: list[list[str]]) -> list[str]:
    texts = [""]
    for choices in parts:
        texts = [text + choice for text in texts for choice in choices][:_MOST_VARIANTS]
    return texts
