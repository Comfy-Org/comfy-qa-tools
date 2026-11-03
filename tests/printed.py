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
