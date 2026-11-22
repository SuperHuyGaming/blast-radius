"""Patch parsing and candidate selection (shared by runtime and offline evaluation)."""

import ast
import re

HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    __slots__ = ("old", "new", "hunks", "is_new", "is_deleted", "binary")

    def __init__(self):
        self.old = None
        self.new = None
        self.hunks = []  # list of (old_start, old_len, new_start, new_len, lines)
        self.is_new = False
        self.is_deleted = False
        self.binary = False


def parse_patch(text):
    files = []
    cur = None
    hunk = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            hunk = None
            parts = line.split(" ")
            if len(parts) >= 4:
                cur.old = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                cur.new = parts[3][2:] if parts[3].startswith("b/") else parts[3]
            continue
        if cur is None and (line.startswith("--- ") or line.startswith("+++ ")):
            cur = FilePatch()
            files.append(cur)
        if hunk is None or not (line[:1] in (" ", "+", "-", "\\") and hunk[5] > 0):
            if line.startswith("--- "):
                p = line[4:].split("\t")[0].strip()
                if p == "/dev/null":
                    cur.is_new = True
                else:
                    cur.old = p[2:] if p.startswith("a/") else p
                hunk = None
                continue
            if line.startswith("+++ "):
                p = line[4:].split("\t")[0].strip()
                if p == "/dev/null":
                    cur.is_deleted = True
                else:
                    cur.new = p[2:] if p.startswith("b/") else p
                hunk = None
                continue
            if line.startswith("new file mode"):
                cur.is_new = True
                continue
            if line.startswith("deleted file mode"):
                cur.is_deleted = True
                continue
            if line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
                continue
            mm = HUNK.match(line)
            if mm and cur is not None:
                os_, ol, ns, nl = mm.groups()
                ol = 1 if ol is None else int(ol)
                nl = 1 if nl is None else int(nl)
                # remaining counter = old+new lines still expected
                hunk = [int(os_), ol, int(ns), nl, [], ol + nl]
                cur.hunks.append(hunk)
                continue
            continue
        # inside a hunk
        tag = line[:1]
        if tag == "\\":
            hunk[4].append(("\\", line))
            continue
        hunk[4].append((tag, line[1:]))
        if tag == " ":
            hunk[5] -= 2
        else:
            hunk[5] -= 1
    return files


def apply_file(old_lines, fp):
    """Apply hunks to a list of lines (without newlines). Returns new lines, or None on mismatch."""
    out = []
    pos = 0  # index into old_lines
    for os_, ol, ns, nl, lines, _ in fp.hunks:
        start = os_ - 1 if ol > 0 else os_
        if start < pos:
            return None
        out.extend(old_lines[pos:start])
        pos = start
        for tag, txt in lines:
            if tag == " ":
                if pos >= len(old_lines) or old_lines[pos] != txt:
                    return None
                out.append(txt)
                pos += 1
            elif tag == "-":
                if pos >= len(old_lines) or old_lines[pos] != txt:
                    return None
                pos += 1
            elif tag == "+":
                out.append(txt)
    out.extend(old_lines[pos:])
    return out


def changed_old_lines(fp):
    """Old-file line numbers touched: removed lines, and both neighbours of insertions."""
    touched = set()
    for os_, ol, ns, nl, lines, _ in fp.hunks:
        old = os_ if ol > 0 else os_ + 1
        prev_minus = False
        for tag, txt in lines:
            if tag == " ":
                old += 1
            elif tag == "-":
                touched.add(old)
                old += 1
            elif tag == "+":
                touched.add(old - 1)
                touched.add(old)
    return touched


def ast_equal(old_bytes, new_bytes):
    try:
        a = ast.parse(old_bytes)
        b = ast.parse(new_bytes)
    except (SyntaxError, ValueError):
        return False
    return ast.dump(a) == ast.dump(b)


def trivial_text_change(fp):
    """True if every added/removed line is blank or starts with '#' (candidate for AST check)."""
    for h in fp.hunks:
        for tag, txt in h[4]:
            if tag in "+-":
                s = txt.strip()
                if s and not s.startswith("#"):
                    return False
    return True


def shallow_insertions(fp):
    """(old anchor line, indent) for added code lines at module or class-body depth."""
    out = []
    for os_, ol, ns, nl, lines, _ in fp.hunks:
        old = os_ if ol > 0 else os_ + 1
        has_minus = any(t == "-" for t, _ in lines)
        for tag, txt in lines:
            if tag == " ":
                old += 1
            elif tag == "-":
                old += 1
            elif tag == "+":
                s = txt.strip()
                if not s or s.startswith("#"):
                    continue
                ind = len(txt) - len(txt.lstrip())
                deflike = s.startswith(("def ", "async def ", "class ", "@"))
                if not has_minus and (ind <= 4 or (ind == 8 and deflike)):
                    out.append((old, ind))
    return out


def class_or_module_users(old_bytes, shallow, info, module_bits):
    """Tests affected by statements inserted at class-body or module level."""
    try:
        tree = ast.parse(old_bytes)
    except (SyntaxError, ValueError, TypeError):
        return module_bits
    b = 0
    for anchor, ind in shallow:
        if ind == 0:
            b |= module_bits
            continue
        for node in tree.body:
            lo = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            if isinstance(node, ast.ClassDef) and lo <= anchor <= node.end_lineno + 1:
                # class header lines carry the users of the class
                if node.lineno < len(info):
                    b |= info[node.lineno]
                break
        else:
            b |= module_bits
    return b


# Enum classes defined in sqlglot (filled from the index at run time). For members of
# these, and for None, `is` and `==` agree: no __eq__ in sqlglot returns true for None,
# and enum members are singletons that the code compares against members.
ENUM_NAMES = set()
_SWAP = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}


def _singleton(node):
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Attribute) and node.attr.isupper():
        v = node.value
        name = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else None
        return name in ENUM_NAMES
    return False


def _same(a, b, seen):
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y, seen) for x, y in zip(a, b))
    if not isinstance(a, ast.AST):
        return a == b
    if isinstance(a, ast.Compare) and len(a.ops) == len(b.ops):
        operands = [a.left] + list(a.comparators)
        for k, (x, y) in enumerate(zip(a.ops, b.ops)):
            if type(x) is not type(y):
                if (type(x), type(y)) not in _SWAP:
                    return False
                if not (_singleton(operands[k]) or _singleton(operands[k + 1])):
                    return False
                seen.append(k)
        return _same(a.left, b.left, seen) and _same(a.comparators, b.comparators, seen)
    for f in a._fields:
        if not _same(getattr(a, f, None), getattr(b, f, None), seen):
            return False
    return True


def equivalent_compare(fp, old_bytes, new_bytes):
    """True if the only change swaps `is`/`==` (or `is not`/`!=`) against None or an enum member."""
    for h in fp.hunks:
        for tag, txt in h[4]:
            if tag in "+-" and txt.strip() and " is " not in txt and "==" not in txt and "!=" not in txt:
                return False
    try:
        a = ast.parse(old_bytes)
        b = ast.parse(new_bytes)
    except (SyntaxError, ValueError):
        return False
    seen = []
    return _same(a, b, seen) and bool(seen)
