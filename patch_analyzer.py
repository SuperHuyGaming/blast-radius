"""Unified diff parser, in-memory patch applier, and AST/semantic/fixture diff analyzer."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_EMPTY_SET: set[Any] = set()

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
CODING_RE = re.compile(r"coding[:=]\s*([-\w.]+)")


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]


@dataclass
class FilePatch:
    old_path: str | None  # None for /dev/null (new file)
    new_path: str | None  # None for /dev/null (deleted file)
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_deleted(self) -> bool:
        return self.new_path is None

    @property
    def is_rename(self) -> bool:
        return self.old_path is not None and self.new_path is not None and self.old_path != self.new_path


def _strip_prefix(raw: str) -> str | None:
    raw = raw.strip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if raw.startswith("a/") or raw.startswith("b/"):
        return raw[2:]
    return raw


def parse_unified_diff(patch_text: str) -> list[FilePatch]:
    """Parse a git unified diff into FilePatch objects."""
    lines = patch_text.splitlines(keepends=True)
    patches: list[FilePatch] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            # Extract fallback paths from header
            header_old = None
            header_new = None
            parts = line.strip().split(" ")
            if len(parts) >= 4:
                header_old = _strip_prefix(parts[2])
                header_new = _strip_prefix(parts[3])
            i += 1
            old_path = header_old
            new_path = header_new
            is_binary = False
            hunks: list[Hunk] = []
            while i < n and not lines[i].startswith("diff --git "):
                cur = lines[i]
                if cur.startswith("--- "):
                    old_path = _strip_prefix(cur[4:])
                    i += 1
                elif cur.startswith("+++ "):
                    new_path = _strip_prefix(cur[4:])
                    i += 1
                elif cur.startswith("new file mode "):
                    old_path = None
                    i += 1
                elif cur.startswith("deleted file mode "):
                    new_path = None
                    i += 1
                elif cur.startswith("rename from "):
                    old_path = _strip_prefix(cur[12:])
                    i += 1
                elif cur.startswith("rename to "):
                    new_path = _strip_prefix(cur[10:])
                    i += 1
                elif cur.startswith("Binary files ") or cur.startswith("GIT binary patch"):
                    is_binary = True
                    i += 1
                elif cur.startswith("@@ "):
                    m = HUNK_RE.match(cur)
                    if not m:
                        i += 1
                        continue
                    old_start = int(m.group(1))
                    old_count = int(m.group(2)) if m.group(2) is not None else 1
                    new_start = int(m.group(3))
                    new_count = int(m.group(4)) if m.group(4) is not None else 1
                    i += 1
                    hunk_lines: list[str] = []
                    while i < n and not lines[i].startswith(("@@ ", "diff --git ")):
                        hl = lines[i]
                        if hl.startswith((" ", "+", "-", "\\")):
                            hunk_lines.append(hl)
                            i += 1
                        else:
                            break
                    hunks.append(Hunk(old_start, old_count, new_start, new_count, hunk_lines))
                else:
                    i += 1
            patches.append(FilePatch(old_path, new_path, hunks, is_binary))
        elif line.startswith("--- "):
            old_path = _strip_prefix(line[4:])
            i += 1
            new_path = None
            if i < n and lines[i].startswith("+++ "):
                new_path = _strip_prefix(lines[i][4:])
                i += 1
            hunks = []
            while i < n and lines[i].startswith("@@ "):
                m = HUNK_RE.match(lines[i])
                if not m:
                    break
                old_start = int(m.group(1))
                old_count = int(m.group(2)) if m.group(2) is not None else 1
                new_start = int(m.group(3))
                new_count = int(m.group(4)) if m.group(4) is not None else 1
                i += 1
                hunk_lines = []
                while i < n and not lines[i].startswith(("@@ ", "diff --git ", "--- ")):
                    hl = lines[i]
                    if hl.startswith((" ", "+", "-", "\\")):
                        hunk_lines.append(hl)
                        i += 1
                    else:
                        break
                hunks.append(Hunk(old_start, old_count, new_start, new_count, hunk_lines))
            patches.append(FilePatch(old_path, new_path, hunks, False))
        else:
            i += 1

    return patches


def apply_file_patch(old_text: str, fp: FilePatch) -> str:
    """Apply parsed unified diff hunks in-memory to old_text."""
    if fp.is_deleted:
        return ""
    if not fp.hunks:
        return old_text

    old_lines = old_text.splitlines(keepends=True) if old_text else []
    out: list[str] = []
    cursor = 0  # 0-based index into old_lines

    for hunk in fp.hunks:
        if hunk.old_count == 0:
            target = max(0, hunk.old_start)
        else:
            target = max(0, hunk.old_start - 1) if hunk.old_start > 0 else 0
        if target > cursor:
            out.extend(old_lines[cursor:target])
            cursor = target

        for idx, hl in enumerate(hunk.lines):
            if hl.startswith("\\"):
                # "\ No newline at end of file" applies to the immediately preceding line
                if idx > 0 and hunk.lines[idx - 1].startswith("+") and out:
                    out[-1] = out[-1].rstrip("\r\n")
                continue
            tag = hl[0]
            body = hl[1:]
            if tag == " ":
                if cursor < len(old_lines):
                    out.append(old_lines[cursor])
                    cursor += 1
                else:
                    out.append(body)
            elif tag == "-":
                if cursor < len(old_lines):
                    cursor += 1
            elif tag == "+":
                out.append(body)

    if cursor < len(old_lines):
        out.extend(old_lines[cursor:])
    return "".join(out)


_CF_KEYWORD_RE = re.compile(
    r"\b(?:return|break|continue|raise|yield|if|elif|else|for|while|try|except|finally|with|match|case|def|class|async)\b"
)
_CF_BRANCH_KWS = frozenset({"elif", "else", "except", "finally", "return", "break", "continue", "raise"})


def _leading_indent(s: str) -> str:
    return s[: len(s) - len(s.lstrip(" \t"))]


def _extract_hunk_lines(fp: FilePatch) -> tuple[set[int], set[int], set[int]]:
    """Return (modified_or_deleted_old_lines, insertion_anchor_old_lines, cf_or_indent_old_lines) 1-based.
    Only suppresses insertion anchors on exact in-place same-indent, same-count, same-control-flow replacements."""
    changed: set[int] = set()
    anchors: set[int] = set()
    cf_lines: set[int] = set()

    for hunk in fp.hunks:
        old_ln = hunk.old_start
        i = 0
        n = len(hunk.lines)
        while i < n:
            hl = hunk.lines[i]
            if hl.startswith("\\"):
                i += 1
                continue
            tag = hl[0]
            if tag == " ":
                old_ln += 1
                i += 1
                continue
            # Collect a contiguous change block of '-' and '+' lines
            start_old_ln = old_ln
            del_bodies: list[str] = []
            add_bodies: list[str] = []
            del_lns: list[int] = []
            while i < n and not hunk.lines[i].startswith(" "):
                cur = hunk.lines[i]
                if cur.startswith("\\"):
                    i += 1
                    continue
                if cur[0] == "-":
                    del_bodies.append(cur[1:])
                    del_lns.append(old_ln)
                    changed.add(old_ln)
                    old_ln += 1
                elif cur[0] == "+":
                    add_bodies.append(cur[1:])
                i += 1

            # Check whether this block is a strict in-place replacement (same line count, indent, blankness, CF keywords)
            is_strict_inplace = (
                len(del_bodies) > 0
                and len(del_bodies) == len(add_bodies)
                and all(
                    _leading_indent(d) == _leading_indent(a)
                    and (d.strip() == "") == (a.strip() == "")
                    and _CF_KEYWORD_RE.findall(d) == _CF_KEYWORD_RE.findall(a)
                    for d, a in zip(del_bodies, add_bodies)
                )
            )
            if not is_strict_inplace:
                # Emit insertion/boundary anchors and mark control-flow/indent lines!
                if start_old_ln > 1:
                    anchors.add(start_old_ln - 1)
                if start_old_ln > 0:
                    anchors.add(start_old_ln)
                if old_ln > 1:
                    anchors.add(old_ln - 1)
                if old_ln > 0:
                    anchors.add(old_ln)
                cf_lines.update(del_lns)
                if start_old_ln > 1:
                    cf_lines.add(start_old_ln - 1)
                if start_old_ln > 0:
                    cf_lines.add(start_old_ln)
                if old_ln > 0:
                    cf_lines.add(old_ln)
            else:
                # Even on same-indent replacement, mark branch/exit keywords
                for d, ln in zip(del_bodies, del_lns):
                    if any(kw in _CF_BRANCH_KWS for kw in _CF_KEYWORD_RE.findall(d)):
                        cf_lines.add(ln)

    return changed, anchors, cf_lines


def extract_changed_old_lines(fp: FilePatch) -> tuple[set[int], set[int]]:
    """Return (modified_or_deleted_old_lines, insertion_anchor_old_lines) 1-based."""
    changed, anchors, _ = _extract_hunk_lines(fp)
    return changed, anchors


@dataclass
class ChangeUnit:
    """One fine-grained semantic change extracted from a modified Python file."""
    kind: str  # "func_body", "dict_key", "class_attr", "module_attr", "module_other", "syntax_error"
    file: str
    class_name: str | None = None
    symbol_name: str | None = None  # func qualname, attr name, or dict attr name
    dict_keys: set[str] | None = None  # None means wildcard "*"
    dict_added_keys: set[str] = field(default_factory=set)
    dict_deleted_keys: set[str] = field(default_factory=set)
    old_lines: set[int] = field(default_factory=set)
    enclosing_lines: set[int] = field(default_factory=set)
    has_nested_func: bool = False
    edit_outside_nested_func: bool = False
    is_cached_func: bool = False


@dataclass
class PyFileAnalysis:
    file: str
    is_noop: bool  # True ONLY when AST is identical and no coding cookie changed
    syntax_error: bool = False
    units: list[ChangeUnit] = field(default_factory=list)
    all_changed_old_lines: set[int] = field(default_factory=set)
    # Functions whose body changed (qualname -> (start_line, end_line))
    changed_funcs: list[str] = field(default_factory=list)


def _strip_docstrings_and_type_checking(tree: ast.AST) -> str:
    """Exact AST dump (preserves docstrings and strings, ignores comments/whitespace)."""
    return ast.dump(tree, include_attributes=False)


def _strip_leading_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return body without a leading bare string-literal docstring statement."""
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _is_sound_cmp_pair(
    left: ast.AST,
    right: ast.AST,
    allow_none: bool,
    pure_enums: set[str],
    strict_enum_attrs: set[tuple[str, str]],
) -> bool:
    """Provably sound identity/equality equivalence pairs verified by full codebase __eq__ audit:
    - None comparisons outside env.py/table.py/builders.py (all sqlglot __eq__ return False on None!)
    - Pure enum.Enum members (Enum.__eq__ is literally `self is other`)
    - Strictly typed enum attributes (e.g. self.normalization_strategy with NormalizationStrategy)."""
    if allow_none and (
        (isinstance(right, ast.Constant) and right.value is None)
        or (isinstance(left, ast.Constant) and left.value is None)
    ):
        return True
    for lhs, rhs in ((left, right), (right, left)):
        if isinstance(rhs, ast.Attribute) and isinstance(rhs.value, ast.Name):
            enum_name = rhs.value.id
            if enum_name in pure_enums:
                return True
            if isinstance(lhs, ast.Attribute) and (lhs.attr, enum_name) in strict_enum_attrs:
                return True
    return False


def _ast_nodes_equiv(
    a: Any,
    b: Any,
    ignore_docstrings: bool = False,
    ignore_type_checking: bool = False,
    allow_sound_cmp: bool = False,
    allow_none: bool = False,
    pure_enums: set[str] | None = None,
    strict_enum_attrs: set[tuple[str, str]] | None = None,
    cmp_site_ok: Any = None,
) -> bool:
    """Zero-copy recursive AST equivalence checker.
    Only allows provably sound Is<->Eq / IsNot<->NotEq pairs when allow_sound_cmp=True or cmp_site_ok!"""
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(
            _ast_nodes_equiv(
                x, y, ignore_docstrings, ignore_type_checking,
                allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok,
            )
            for x, y in zip(a, b)
        )
    if (allow_sound_cmp or cmp_site_ok is not None) and isinstance(a, ast.Compare):
        if len(a.ops) == 1 and len(b.ops) == 1 and len(a.comparators) == 1 and len(b.comparators) == 1:
            op_a, op_b = a.ops[0], b.ops[0]
            if type(op_a) is not type(op_b):
                eq_is_pair = (
                    (isinstance(op_a, ast.Is) and isinstance(op_b, ast.Eq))
                    or (isinstance(op_a, ast.Eq) and isinstance(op_b, ast.Is))
                    or (isinstance(op_a, ast.IsNot) and isinstance(op_b, ast.NotEq))
                    or (isinstance(op_a, ast.NotEq) and isinstance(op_b, ast.IsNot))
                )
                site_safe = (
                    (cmp_site_ok is not None and cmp_site_ok(a.lineno, a.col_offset))
                    or (
                        allow_sound_cmp
                        and _is_sound_cmp_pair(
                            a.left, a.comparators[0], allow_none,
                            pure_enums or _EMPTY_SET, strict_enum_attrs or _EMPTY_SET,
                        )
                    )
                )
                if not (
                    eq_is_pair
                    and site_safe
                    and _ast_nodes_equiv(
                        a.left, b.left, ignore_docstrings, ignore_type_checking,
                        allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok,
                    )
                    and _ast_nodes_equiv(
                        a.comparators[0], b.comparators[0], ignore_docstrings, ignore_type_checking,
                        allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok,
                    )
                ):
                    return False
                return True
    if isinstance(a, ast.AST):
        is_doc_owner = ignore_docstrings and isinstance(a, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        is_tc_owner = ignore_type_checking and isinstance(a, (ast.Module, ast.ClassDef))
        for field_name in a._fields:
            va = getattr(a, field_name)
            vb = getattr(b, field_name)
            if field_name == "body":
                if is_doc_owner:
                    va = _strip_leading_docstring(va)
                    vb = _strip_leading_docstring(vb)
                if is_tc_owner:
                    va = [s for s in va if not _is_type_checking_if(s)]
                    vb = [s for s in vb if not _is_type_checking_if(s)]
            if not _ast_nodes_equiv(
                va, vb, ignore_docstrings, ignore_type_checking,
                allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok,
            ):
                return False
        return True
    return a == b


def _stmt_start_line(node: ast.stmt) -> int:
    start = node.lineno
    for d in getattr(node, "decorator_list", None) or ():
        if d.lineno < start:
            start = d.lineno
    return start


def _same_stmt_text(x: ast.stmt, y: ast.stmt, old_lines: list[str], new_lines: list[str]) -> bool:
    """True when two statements span byte-identical source text, hence parse to identical ASTs."""
    if type(x) is not type(y) or x.col_offset != y.col_offset or x.end_col_offset != y.end_col_offset:
        return False
    xs = _stmt_start_line(x)
    ys = _stmt_start_line(y)
    xe = x.end_lineno
    ye = y.end_lineno
    if xe is None or ye is None or xe - xs != ye - ys:
        return False
    return old_lines[xs - 1 : xe] == new_lines[ys - 1 : ye]


def _fast_tree_equiv(
    a: Any,
    b: Any,
    old_lines: list[str] | None,
    new_lines: list[str] | None,
    ignore_docstrings: bool = False,
    ignore_type_checking: bool = False,
    allow_sound_cmp: bool = False,
    allow_none: bool = False,
    pure_enums: set[str] | None = None,
    strict_enum_attrs: set[tuple[str, str]] | None = None,
    cmp_site_ok: Any = None,
) -> bool:
    """Exactly _ast_nodes_equiv(a, b, ...) for old/new Module trees, but statements (at module and
    class level) whose source text is identical are skipped instead of walked node by node."""
    rest = (
        ignore_docstrings,
        ignore_type_checking,
        allow_sound_cmp,
        allow_none,
        pure_enums,
        strict_enum_attrs,
        cmp_site_ok,
    )
    if old_lines is None or new_lines is None or not isinstance(a, (ast.Module, ast.ClassDef)):
        return _ast_nodes_equiv(a, b, *rest)
    if type(a) is not type(b):
        return False
    for field_name in a._fields:
        if field_name != "body" and not _ast_nodes_equiv(getattr(a, field_name), getattr(b, field_name), *rest):
            return False
    va = a.body
    vb = b.body
    if ignore_docstrings:
        va = _strip_leading_docstring(va)
        vb = _strip_leading_docstring(vb)
    if ignore_type_checking:
        va = [st for st in va if not _is_type_checking_if(st)]
        vb = [st for st in vb if not _is_type_checking_if(st)]
    if len(va) != len(vb):
        return False
    for x, y in zip(va, vb):
        if _same_stmt_text(x, y, old_lines, new_lines):
            continue
        if isinstance(x, ast.ClassDef) and isinstance(y, ast.ClassDef):
            if not _fast_tree_equiv(x, y, old_lines, new_lines, *rest):
                return False
        elif not _ast_nodes_equiv(x, y, *rest):
            return False
    return True


def _source_lines(src: str) -> list[str] | None:
    if "\r" in src:
        return None
    return src.split("\n")


def _diff_has_identity_or_eq_change(fp: FilePatch) -> bool:
    for h in fp.hunks:
        for line in h.lines:
            if line.startswith(("+", "-")) and (" is " in line or "==" in line or "!=" in line or "None" in line):
                return True
    return False


_UNSAFE_NONE_EQ_FILES = frozenset({
    "sqlglot/executor/env.py",
    "sqlglot/executor/table.py",
    "sqlglot/expressions/builders.py",
})


def _is_sound_cmp_equiv(
    file: str,
    old_ast: ast.AST,
    new_ast: ast.AST,
    fp: FilePatch,
    pure_enums: set[str],
    strict_enum_attrs: set[tuple[str, str]],
    ignore_docstrings: bool = False,
    old_lines: list[str] | None = None,
    new_lines: list[str] | None = None,
) -> bool:
    if not file.startswith("sqlglot/") or not _diff_has_identity_or_eq_change(fp):
        return False
    allow_none = file not in _UNSAFE_NONE_EQ_FILES
    return _fast_tree_equiv(
        old_ast, new_ast, old_lines, new_lines,
        ignore_docstrings=ignore_docstrings,
        ignore_type_checking=False,
        allow_sound_cmp=True,
        allow_none=allow_none,
        pure_enums=pure_enums,
        strict_enum_attrs=strict_enum_attrs,
    )


def _coding_cookie(src: str) -> str | None:
    for line in src.splitlines()[:2]:
        m = CODING_RE.search(line)
        if m:
            return m.group(1).lower()
    return None


def _const_key_repr(node: ast.AST | None) -> str | None:
    """Resolve a static dict key AST node to a canonical string key."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool)) or node.value is None:
            return repr(node.value)
        return None
    if isinstance(node, ast.Attribute):
        # e.g. exp.Array, exp.DType.INT, TokenType.VAR
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            parts.reverse()
            return ".".join(parts)
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Tuple):
        elts = [_const_key_repr(e) for e in node.elts]
        if any(e is None for e in elts):
            return None
        return "(" + ", ".join(elts) + ")"  # type: ignore[arg-type]
    return None


def _diff_dict_nodes_detailed(
    old_dict: ast.Dict, new_dict: ast.Dict
) -> tuple[set[str] | None, set[str], set[str], set[int] | None]:
    """Return (changed_keys_or_None_wildcard, added_keys, deleted_keys, lambda_only_old_lines_or_None)."""
    old_unpacks: list[str] = []
    old_map: dict[str, list[tuple[int, str, ast.AST]]] = {}
    seg = 0
    for k, v in zip(old_dict.keys, old_dict.values):
        if k is None:
            old_unpacks.append(ast.dump(v, include_attributes=False))
            seg += 1
        else:
            kr = _const_key_repr(k)
            if kr is None:
                return None, set(), set(), None
            old_map.setdefault(kr, []).append((seg, ast.dump(v, include_attributes=False), v))

    new_unpacks: list[str] = []
    new_map: dict[str, list[tuple[int, str, ast.AST]]] = {}
    seg = 0
    for k, v in zip(new_dict.keys, new_dict.values):
        if k is None:
            new_unpacks.append(ast.dump(v, include_attributes=False))
            seg += 1
        else:
            kr = _const_key_repr(k)
            if kr is None:
                return None, set(), set(), None
            new_map.setdefault(kr, []).append((seg, ast.dump(v, include_attributes=False), v))

    if old_unpacks != new_unpacks:
        return None, set(), set(), None

    old_keys = set(old_map)
    new_keys = set(new_map)
    added_keys = new_keys - old_keys
    deleted_keys = old_keys - new_keys
    changed_keys = {
        k
        for k in old_keys | new_keys
        if [(s, d) for s, d, _ in old_map.get(k, ())] != [(s, d) for s, d, _ in new_map.get(k, ())]
    }
    # If AST differed (e.g., key reordering in TIME_MAPPING / QUOTES!)
    # yet no individual key/value/segment changed, return wildcard None!
    if not changed_keys:
        return None, set(), set(), None

    # Check if EVERY changed key is a pure in-place ast.Lambda body edit (same keyset, same lambda args!)
    lambda_only_lines: set[int] | None = None
    if not added_keys and not deleted_keys:
        lam_lines: set[int] = set()
        all_lam = True
        for k in changed_keys:
            ol = old_map[k]
            nl = new_map[k]
            if (
                len(ol) == 1
                and len(nl) == 1
                and ol[0][0] == nl[0][0]
                and isinstance(ol[0][2], ast.Lambda)
                and isinstance(nl[0][2], ast.Lambda)
                and ast.dump(ol[0][2].args, include_attributes=False)
                == ast.dump(nl[0][2].args, include_attributes=False)
            ):
                lam_lines |= _stmt_line_span(ol[0][2].body)
            else:
                all_lam = False
                break
        if all_lam and lam_lines:
            lambda_only_lines = lam_lines

    return changed_keys, added_keys, deleted_keys, lambda_only_lines


def _stmt_line_span(node: ast.AST) -> set[int]:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", start)
    if start is None:
        return set()
    return set(range(start, (end or start) + 1))


_TRY_TYPES: tuple[type[ast.stmt], ...] = (
    (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)
)
_MATCH_TYPES: tuple[type[ast.stmt], ...] = (
    (ast.Match,) if hasattr(ast, "Match") else ()
)
_COMPOUND_TYPES: tuple[type[ast.stmt], ...] = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
    *_TRY_TYPES, *_MATCH_TYPES,
)


def _stmt_entry_lines(stmt: ast.stmt) -> set[int]:
    """Executable entry lines of a statement (handles Python 3.13 `try:` emitting no LINE event!)."""
    if isinstance(stmt, ast.If):
        return _stmt_line_span(stmt.test) | {stmt.lineno}
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return _stmt_line_span(stmt.target) | _stmt_line_span(stmt.iter) | {stmt.lineno}
    if isinstance(stmt, ast.While):
        return _stmt_line_span(stmt.test) | {stmt.lineno}
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        hdr = {stmt.lineno}
        for item in stmt.items:
            hdr |= _stmt_line_span(item)
        return hdr
    if isinstance(stmt, _TRY_TYPES):
        # CPython 3.13 zero-cost exceptions emit NO LINE event on `try:` itself!
        hdr = {stmt.lineno}
        if stmt.body:
            hdr |= _stmt_entry_lines(stmt.body[0])
        return hdr
    if _MATCH_TYPES and isinstance(stmt, _MATCH_TYPES):
        return _stmt_line_span(stmt.subject) | {stmt.lineno}  # type: ignore[attr-defined]
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        first_body_ln = getattr(stmt.body[0], "lineno", stmt.lineno + 1) if stmt.body else stmt.lineno + 1
        dec_min = min((getattr(d, "lineno", stmt.lineno) for d in stmt.decorator_list), default=stmt.lineno)
        return set(range(dec_min, max(stmt.lineno + 1, first_body_ln)))
    return _stmt_line_span(stmt)


def _collect_struct_enclosing_headers(
    old_body: list[ast.stmt],
    new_body: list[ast.stmt],
    stmt_lines: set[int],
    enclosing_hdr: set[int],
    ignore_docstrings: bool = False,
) -> None:
    """AST-level structural comparison of two statement blocks: if block length, statement types,
    or compound branch presence (orelse/finalbody/handlers/cases) changed, add enclosing header entry lines!"""
    if ignore_docstrings:
        old_body = _strip_leading_docstring(old_body)
        new_body = _strip_leading_docstring(new_body)

    if len(old_body) != len(new_body):
        stmt_lines |= enclosing_hdr

    for o_s, n_s in zip(old_body, new_body):
        if type(o_s) is not type(n_s):
            stmt_lines |= enclosing_hdr | _stmt_entry_lines(o_s)
            continue
        if isinstance(o_s, _COMPOUND_TYPES):
            sub_hdr = _stmt_entry_lines(o_s)
            if hasattr(o_s, "body") and hasattr(n_s, "body"):
                _collect_struct_enclosing_headers(o_s.body, n_s.body, stmt_lines, sub_hdr, False)
            if hasattr(o_s, "orelse") and hasattr(n_s, "orelse"):
                if bool(o_s.orelse) != bool(n_s.orelse):
                    stmt_lines |= sub_hdr
                elif o_s.orelse:
                    _collect_struct_enclosing_headers(o_s.orelse, n_s.orelse, stmt_lines, sub_hdr, False)
            if hasattr(o_s, "finalbody") and hasattr(n_s, "finalbody"):
                if bool(o_s.finalbody) != bool(n_s.finalbody):
                    stmt_lines |= sub_hdr
                elif o_s.finalbody:
                    _collect_struct_enclosing_headers(o_s.finalbody, n_s.finalbody, stmt_lines, sub_hdr, False)
            if isinstance(o_s, _TRY_TYPES) and isinstance(n_s, _TRY_TYPES):
                if len(o_s.handlers) != len(n_s.handlers):
                    stmt_lines |= sub_hdr
                    for oh in o_s.handlers:
                        stmt_lines.add(oh.lineno)
                else:
                    for oh, nh in zip(o_s.handlers, n_s.handlers):
                        h_hdr = sub_hdr | {oh.lineno} | (_stmt_line_span(oh.type) if oh.type is not None else set())
                        _collect_struct_enclosing_headers(oh.body, nh.body, stmt_lines, h_hdr, False)
            if _MATCH_TYPES and isinstance(o_s, _MATCH_TYPES) and isinstance(n_s, _MATCH_TYPES):
                o_cases = o_s.cases  # type: ignore[attr-defined]
                n_cases = n_s.cases  # type: ignore[attr-defined]
                if len(o_cases) != len(n_cases):
                    stmt_lines |= sub_hdr
                else:
                    for oc, nc in zip(o_cases, n_cases):
                        _collect_struct_enclosing_headers(oc.body, nc.body, stmt_lines, sub_hdr, False)
        elif isinstance(o_s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            sub_hdr = _stmt_entry_lines(o_s)
            _collect_struct_enclosing_headers(o_s.body, n_s.body, stmt_lines, sub_hdr, ignore_docstrings)


def _func_signature_dump(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Dump everything about a function EXCEPT its body statements (distinguishes def vs async def!)."""
    clone = type(fn)(
        name=fn.name,
        args=fn.args,
        body=[],
        decorator_list=fn.decorator_list,
        returns=fn.returns,
        type_params=getattr(fn, "type_params", []),
    )
    return ast.dump(clone, include_attributes=False)


_CACHE_DECORATORS = frozenset({"cache", "lru_cache", "cached_property"})


def _is_cached_func(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id in _CACHE_DECORATORS:
            return True
        if isinstance(target, ast.Attribute) and target.attr in _CACHE_DECORATORS:
            return True
    return False


def _extract_assign_target_and_value(stmt: ast.stmt) -> tuple[str | None, ast.AST | None]:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return stmt.targets[0].id, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id, stmt.value
    return None, None


def _is_type_checking_if(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.If) or stmt.orelse:
        return False
    test = stmt.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _is_overload_fn(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in stmt.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "overload":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "overload":
            return True
    return False


def _stmt_src_equal(
    o_node: ast.AST,
    n_node: ast.AST,
    old_lines_arr: list[str],
    new_lines_arr: list[str],
) -> bool:
    o1 = getattr(o_node, "lineno", None)
    o2 = getattr(o_node, "end_lineno", None)
    n1 = getattr(n_node, "lineno", None)
    n2 = getattr(n_node, "end_lineno", None)
    if o1 is None or o2 is None or n1 is None or n2 is None:
        return False
    if (o2 - o1) != (n2 - n1):
        return False
    return old_lines_arr[o1 - 1 : o2] == new_lines_arr[n1 - 1 : n2]


def _compare_block(
    file: str,
    old_body: list[ast.stmt],
    new_body: list[ast.stmt],
    old_lines_arr: list[str],
    new_lines_arr: list[str],
    class_chain: list[str],
    changed_old_lines: set[int],
    anchor_old_lines: set[int],
    cf_old_lines: set[int],
    units: list[ChangeUnit],
    changed_funcs: list[str],
    ignore_docstrings: bool = False,
) -> None:
    """Compare statements in a module or class body and emit precise ChangeUnits."""
    if ignore_docstrings:
        old_body = _strip_leading_docstring(old_body)
        new_body = _strip_leading_docstring(new_body)

    def stmt_key(s: ast.stmt) -> tuple[str, str] | None:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ("func", s.name)
        if isinstance(s, ast.ClassDef):
            return ("class", s.name)
        name, _ = _extract_assign_target_and_value(s)
        if name is not None:
            return ("assign", name)
        return None

    # Check if statement sequence structure matches cleanly by keyed defs/classes/assigns
    old_by_key: dict[tuple[str, str], list[ast.stmt]] = {}
    new_by_key: dict[tuple[str, str], list[ast.stmt]] = {}
    old_unkeyed: list[ast.stmt] = []
    new_unkeyed: list[ast.stmt] = []
    old_order: list[tuple[str, str]] = []
    new_order: list[tuple[str, str]] = []

    for s in old_body:
        if _is_type_checking_if(s):
            continue
        k = stmt_key(s)
        if k is None:
            old_unkeyed.append(s)
            old_order.append(("unkeyed", ""))
        else:
            old_by_key.setdefault(k, []).append(s)
            old_order.append(k)

    for s in new_body:
        if _is_type_checking_if(s):
            continue
        k = stmt_key(s)
        if k is None:
            new_unkeyed.append(s)
            new_order.append(("unkeyed", ""))
        else:
            new_by_key.setdefault(k, []).append(s)
            new_order.append(k)

    class_qual = ".".join(class_chain) if class_chain else None

    # Any change to unkeyed statements (imports, if-blocks, bare expressions, etc.)
    # OR relative statement reordering of surviving statements (e.g. moving KEYWORDS.pop above KEYWORDS = {...})!
    common_keys = set(old_order) & set(new_order)
    old_common = [k for k in old_order if k in common_keys]
    new_common = [k for k in new_order if k in common_keys]
    if (
        not _ast_nodes_equiv(old_unkeyed, new_unkeyed, ignore_docstrings)
        or old_common != new_common
    ):
        units.append(
            ChangeUnit(
                kind="class_attr" if class_qual else "module_other",
                file=file,
                class_name=class_qual,
                symbol_name="*",
                old_lines=set(changed_old_lines | anchor_old_lines),
            )
        )

    for key in set(old_by_key) | set(new_by_key):
        o_list = old_by_key.get(key, [])
        n_list = new_by_key.get(key, [])
        if len(o_list) == 1 and len(n_list) == 1 and _stmt_src_equal(o_list[0], n_list[0], old_lines_arr, new_lines_arr):
            continue
        if _ast_nodes_equiv(o_list, n_list, ignore_docstrings):
            continue

        kind_tag, name = key
        # Handle @t.overload functions cleanly: if all overload stubs match and only the
        # final implementation def changed, compare the implementation def as a single func!
        if (
            kind_tag == "func"
            and len(o_list) > 1
            and len(o_list) == len(n_list)
            and all(_is_overload_fn(s) for s in o_list[:-1])
            and all(_is_overload_fn(s) for s in n_list[:-1])
            and not _is_overload_fn(o_list[-1])
            and not _is_overload_fn(n_list[-1])
            and _ast_nodes_equiv(o_list[:-1], n_list[:-1], ignore_docstrings)
        ):
            o_list = [o_list[-1]]
            n_list = [n_list[-1]]

        if len(o_list) != 1 or len(n_list) != 1:
            # Added, deleted, or duplicated definition
            span = set()
            for s in o_list:
                span |= _stmt_line_span(s)
            if kind_tag == "func":
                qname = f"{class_qual}.{name}" if class_qual else name
                changed_funcs.append(qname)
                is_dyn_sql_meth = (
                    bool(class_qual)
                    and name.endswith("_sql")
                    and not name.startswith("_")
                    and name != "sql"
                )
                units.append(
                    ChangeUnit(
                        kind="class_attr" if class_qual else "module_other",
                        file=file,
                        class_name=class_qual,
                        # Preserve dynamic <expr_key>_sql method names for exact expr_to_tests pruning;
                        # other added/deleted methods or top-level functions use wildcard symbol "*"
                        symbol_name=name if is_dyn_sql_meth else "*",
                        old_lines=span & (changed_old_lines | anchor_old_lines),
                        enclosing_lines=span,
                    )
                )
            elif kind_tag == "class":
                sub_qual = f"{class_qual}.{name}" if class_qual else name
                units.append(
                    ChangeUnit(
                        kind="class_attr",
                        file=file,
                        class_name=sub_qual,
                        symbol_name="*",
                        old_lines=span,
                        enclosing_lines=span,
                    )
                )
            else:
                units.append(
                    ChangeUnit(
                        kind="class_attr" if class_qual else "module_attr",
                        file=file,
                        class_name=class_qual,
                        symbol_name=name,
                        old_lines=span,
                        enclosing_lines=span,
                    )
                )
            continue

        os_node = o_list[0]
        ns_node = n_list[0]

        if kind_tag == "class":
            assert isinstance(os_node, ast.ClassDef) and isinstance(ns_node, ast.ClassDef)
            sub_chain = [*class_chain, name]
            sub_qual = ".".join(sub_chain)
            # Check bases/keywords/decorators
            if (
                not _ast_nodes_equiv(os_node.bases, ns_node.bases)
                or not _ast_nodes_equiv(os_node.keywords, ns_node.keywords)
                or not _ast_nodes_equiv(os_node.decorator_list, ns_node.decorator_list)
            ):
                units.append(
                    ChangeUnit(
                        kind="class_attr",
                        file=file,
                        class_name=sub_qual,
                        symbol_name="*",
                        old_lines=_stmt_line_span(os_node),
                    )
                )
            _compare_block(
                file,
                os_node.body,
                ns_node.body,
                old_lines_arr,
                new_lines_arr,
                sub_chain,
                changed_old_lines,
                anchor_old_lines,
                cf_old_lines,
                units,
                changed_funcs,
                ignore_docstrings,
            )

        elif kind_tag == "func":
            assert isinstance(os_node, (ast.FunctionDef, ast.AsyncFunctionDef))
            assert isinstance(ns_node, (ast.FunctionDef, ast.AsyncFunctionDef))
            qname = f"{class_qual}.{name}" if class_qual else name
            changed_funcs.append(qname)
            fn_span = _stmt_line_span(os_node)
            sig_changed = _func_signature_dump(os_node) != _func_signature_dump(ns_node)
            # Detect nested defs/lambdas & caching decorators (for import-time / closure soundness)
            inner_defs = [
                n for n in ast.walk(os_node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not os_node
            ]
            new_inner_defs = [
                n for n in ast.walk(ns_node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not ns_node
            ]
            has_nested_func = bool(
                inner_defs
                or new_inner_defs
                or any(isinstance(n, ast.Lambda) for n in ast.walk(os_node))
                or any(isinstance(n, ast.Lambda) for n in ast.walk(ns_node))
            )
            is_cached = _is_cached_func(os_node) or _is_cached_func(ns_node)

            # Find the tightest changed statement spans inside old function body
            hit_lines = fn_span & (changed_old_lines | anchor_old_lines)
            cf_hit = fn_span & cf_old_lines
            inner_body_lines: set[int] = set()
            for idn in inner_defs:
                for is_stmt in idn.body:
                    inner_body_lines |= _stmt_line_span(is_stmt)
            edit_outside_nested = True if not inner_defs else bool((hit_lines | cf_hit) - inner_body_lines)

            stmt_lines: set[int] = set()
            # AST-level structural comparison catches dedents, branch edits, elif->if, statement type swaps!
            _collect_struct_enclosing_headers(
                os_node.body,
                ns_node.body,
                stmt_lines,
                _stmt_entry_lines(os_node.body[0]) if os_node.body else {os_node.lineno},
                ignore_docstrings,
            )
            for sub in ast.walk(os_node):
                if isinstance(sub, ast.stmt) and sub is not os_node:
                    sp = _stmt_line_span(sub)
                    if sp & hit_lines:
                        if isinstance(sub, _COMPOUND_TYPES):
                            hdr = _stmt_entry_lines(sub)
                            if (hdr & hit_lines) or (sp & cf_hit):
                                stmt_lines |= hdr
                            if isinstance(sub, _TRY_TYPES):
                                for h in sub.handlers:
                                    h_hdr = {h.lineno} | (_stmt_line_span(h.type) if h.type is not None else set())
                                    if (h_hdr & hit_lines) or (sp & cf_hit):
                                        stmt_lines |= h_hdr
                        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            hdr = _stmt_entry_lines(sub)
                            if (hdr & hit_lines) or (sp & cf_hit):
                                stmt_lines |= hdr
                        else:
                            stmt_lines |= sp
                # Block-aware insertion anchors: bind to executable entry lines of neighboring statements!
                for attr in ("body", "orelse", "finalbody"):
                    raw_blk = getattr(sub, attr, None)
                    if isinstance(raw_blk, list) and raw_blk:
                        blk = _strip_leading_docstring(raw_blk) if attr == "body" else raw_blk
                        if not blk:
                            continue
                        for idx_s, s_item in enumerate(blk):
                            s_start = getattr(s_item, "lineno", 0)
                            s_end = getattr(s_item, "end_lineno", s_start)
                            prev_end = getattr(blk[idx_s - 1], "end_lineno", 0) if idx_s > 0 else getattr(sub, "lineno", 0)
                            if any(prev_end <= al <= s_start for al in anchor_old_lines):
                                stmt_lines |= _stmt_entry_lines(s_item)
                                if idx_s > 0:
                                    stmt_lines |= _stmt_entry_lines(blk[idx_s - 1])
                            if idx_s == len(blk) - 1:
                                block_end = getattr(sub, "end_lineno", s_end)
                                if any(s_end <= al <= block_end + 1 for al in anchor_old_lines):
                                    stmt_lines |= _stmt_entry_lines(s_item)
            if not stmt_lines or sig_changed:
                stmt_lines = set(fn_span)
            units.append(
                ChangeUnit(
                    kind="class_attr" if (sig_changed and class_qual) else ("module_other" if sig_changed else "func_body"),
                    file=file,
                    class_name=class_qual,
                    symbol_name="*" if sig_changed else qname,
                    old_lines=hit_lines | stmt_lines,
                    enclosing_lines=fn_span,
                    has_nested_func=has_nested_func,
                    edit_outside_nested_func=edit_outside_nested,
                    is_cached_func=is_cached,
                )
            )

        elif kind_tag == "assign":
            _, o_val = _extract_assign_target_and_value(os_node)
            _, n_val = _extract_assign_target_and_value(ns_node)
            span = _stmt_line_span(os_node)
            if isinstance(o_val, ast.Dict) and isinstance(n_val, ast.Dict):
                diff_keys, added_keys, deleted_keys, lam_lines = _diff_dict_nodes_detailed(o_val, n_val)
                units.append(
                    ChangeUnit(
                        kind="dict_lambda_body" if lam_lines is not None else "dict_key",
                        file=file,
                        class_name=class_qual,
                        symbol_name=name,
                        dict_keys=diff_keys,
                        dict_added_keys=added_keys,
                        dict_deleted_keys=deleted_keys,
                        old_lines=lam_lines if lam_lines is not None else (span & (changed_old_lines | anchor_old_lines)),
                        enclosing_lines=span,
                    )
                )
            else:
                units.append(
                    ChangeUnit(
                        kind="class_attr" if class_qual else "module_attr",
                        file=file,
                        class_name=class_qual,
                        symbol_name=name,
                        old_lines=span & (changed_old_lines | anchor_old_lines),
                        enclosing_lines=span,
                    )
                )


def analyze_python_change(
    file: str,
    old_src: str,
    new_src: str,
    fp: FilePatch,
    *,
    old_ast_cache: dict[str, ast.Module] | None = None,
    pure_enums: set[str] | None = None,
    strict_enum_attrs: set[tuple[str, str]] | None = None,
    ignore_docstrings: bool = False,
    cmp_agree: frozenset[str] | None = None,
) -> PyFileAnalysis:
    """Analyze a modified Python file into semantic ChangeUnits."""
    changed_old, anchor_old, cf_old = _extract_hunk_lines(fp)
    if fp.is_new or fp.is_deleted:
        try:
            if not fp.is_deleted:
                ast.parse(new_src.encode("utf-8"), filename=file)
        except SyntaxError:
            return PyFileAnalysis(
                file=file,
                is_noop=False,
                syntax_error=True,
                units=[ChangeUnit(kind="syntax_error", file=file)],
            )
        return PyFileAnalysis(
            file=file,
            is_noop=False,
            units=[ChangeUnit(kind="module_other", file=file, symbol_name="*")],
        )

    try:
        new_ast = ast.parse(new_src.encode("utf-8"), filename=file)
    except SyntaxError:
        return PyFileAnalysis(
            file=file,
            is_noop=False,
            syntax_error=True,
            units=[ChangeUnit(kind="syntax_error", file=file)],
            all_changed_old_lines=changed_old,
        )

    if old_ast_cache is not None and file in old_ast_cache:
        old_ast = old_ast_cache[file]
    else:
        old_ast = ast.parse(old_src, filename=file)
        if old_ast_cache is not None:
            old_ast_cache[file] = old_ast

    # Treat as static no-op when exact AST is identical (or provably sound None/strict-Enum is<->== swap)
    # and no coding cookie changed.
    old_lines = _source_lines(old_src)
    new_lines = _source_lines(new_src)
    if _coding_cookie(old_src) == _coding_cookie(new_src):
        if _fast_tree_equiv(old_ast, new_ast, old_lines, new_lines, ignore_docstrings):
            return PyFileAnalysis(file=file, is_noop=True)
        if _is_sound_cmp_equiv(
            file,
            old_ast,
            new_ast,
            fp,
            pure_enums or _EMPTY_SET,
            strict_enum_attrs or _EMPTY_SET,
            ignore_docstrings=ignore_docstrings,
            old_lines=old_lines,
            new_lines=new_lines,
        ):
            return PyFileAnalysis(file=file, is_noop=True)
        if cmp_agree and _diff_has_identity_or_eq_change(fp) and _fast_tree_equiv(
            old_ast,
            new_ast,
            old_lines,
            new_lines,
            ignore_docstrings,
            cmp_site_ok=lambda line, col: f"{file}:{line}:{col}" in cmp_agree,
        ):
            return PyFileAnalysis(file=file, is_noop=True)

    units: list[ChangeUnit] = []
    changed_funcs: list[str] = []
    _compare_block(
        file,
        old_ast.body,
        new_ast.body,
        old_src.splitlines(),
        new_src.splitlines(),
        [],
        changed_old,
        anchor_old,
        cf_old,
        units,
        changed_funcs,
        ignore_docstrings,
    )

    # Only treat empty `units` as no-op if the ENTIRE difference was strictly inside `if TYPE_CHECKING:`
    # blocks (without orelse) AND file is not in sqlglot/typing/ (which test_typing.py parses!).
    if not units:
        if (
            not file.startswith("sqlglot/typing/")
            and _coding_cookie(old_src) == _coding_cookie(new_src)
            and _fast_tree_equiv(old_ast, new_ast, old_lines, new_lines, ignore_docstrings, ignore_type_checking=True)
        ):
            return PyFileAnalysis(file=file, is_noop=True)
        units.append(ChangeUnit(kind="module_other", file=file, symbol_name="*", old_lines=changed_old))

    return PyFileAnalysis(
        file=file,
        is_noop=False,
        units=units,
        all_changed_old_lines=changed_old | anchor_old,
        changed_funcs=changed_funcs,
    )


def diff_sql_fixture_pairs(old_text: str, new_text: str) -> tuple[bool, list[int]]:
    """Compare two load_sql_fixture_pairs .sql files.

    Returns (parse_error_or_shifted, changed_1based_case_indices).
    If parse_error_or_shifted is True, all cases in the fixture should be checked (or test immediately fails).
    """
    def _parse(text: str) -> list[tuple[dict[str, str], str, str]]:
        filtered = "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))
        stmts = filtered.split(";")
        out = []
        for i in range(0, len(stmts), 2):
            if i + 1 < len(stmts):
                sql = stmts[i].strip()
                meta: dict[str, str] = {}
                sql_lines = sql.split("\n")
                j = 0
                while j < len(sql_lines) and sql_lines[j].startswith("#"):
                    key, val = sql_lines[j].split(":", maxsplit=1)
                    meta[key.lstrip("#").strip()] = val.strip()
                    j += 1
                sql = "\n".join(sql_lines[j:])
                expected = stmts[i + 1].strip()
                out.append((meta, sql, expected))
        return out

    old_cases = _parse(old_text)
    try:
        new_cases = _parse(new_text)
    except Exception:
        return True, []

    if len(old_cases) != len(new_cases):
        return True, []

    changed = [idx for idx, (o, n) in enumerate(zip(old_cases, new_cases), start=1) if o != n]
    return False, changed


def diff_sql_fixture_lines(old_text: str, new_text: str) -> tuple[bool, list[int]]:
    """Compare two load_sql_fixtures .sql files (identity.sql, pretty.sql, etc.)."""
    def _parse(text: str) -> list[str]:
        filtered = "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))
        out = []
        buf: list[str] = []
        for line in filtered.splitlines():
            buf.append(line)
            if not line.startswith("#"):
                out.append("\n".join(buf))
                buf.clear()
        return out

    old_cases = _parse(old_text)
    new_cases = _parse(new_text)
    if len(old_cases) != len(new_cases):
        return True, []
    changed = [idx for idx, (o, n) in enumerate(zip(old_cases, new_cases), start=1) if o != n]
    return False, changed
