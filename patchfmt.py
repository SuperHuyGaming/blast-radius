"""Reading unified diffs and applying them to text held in memory."""

import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FilePatch:
    __slots__ = ("old", "new", "hunks", "binary", "mode_only")

    def __init__(self):
        self.old = None  # path before, None when the file is created
        self.new = None  # path after, None when the file is deleted
        self.hunks = []  # (old_start, old_len, new_start, new_len, [(tag, text)])
        self.binary = False
        self.mode_only = True

    @property
    def path(self):
        return self.old or self.new


class PatchError(Exception):
    pass


def _strip(p):
    p = p.rstrip("\n")
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse(text):
    files = []
    cur = None
    lines = text.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m and m.group(1) == m.group(2):
                cur.old = cur.new = m.group(1)
            elif m:
                # paths differ (rename) or contain " b/": the rename lines below settle it
                half = (len(line) - len("diff --git ")) // 2
                cur.old = line[len("diff --git a/") : len("diff --git ") + half]
                cur.new = line[len("diff --git ") + half + 3 :]
            i += 1
            continue
        if cur is None:
            if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
                cur = FilePatch()
                files.append(cur)
            else:
                i += 1
                continue
        if line.startswith("rename from "):
            cur.old = line[len("rename from ") :]
            cur.mode_only = False
        elif line.startswith("rename to "):
            cur.new = line[len("rename to ") :]
            cur.mode_only = False
        elif line.startswith("copy from "):
            cur.old = None
            cur.mode_only = False
        elif line.startswith("copy to "):
            cur.new = line[len("copy to ") :]
        elif line.startswith("deleted file mode"):
            cur.new = None
            cur.mode_only = False
        elif line.startswith("new file mode"):
            cur.old = None
            cur.mode_only = False
        elif line.startswith("GIT binary patch") or line.startswith("Binary files "):
            cur.binary = True
            cur.mode_only = False
        elif line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            o, w = _strip(line[4:]), _strip(lines[i + 1][4:])
            if o is None:
                cur.old = None
            elif cur.old is None or cur.old == cur.new:
                cur.old = o
            if w is None:
                cur.new = None
            else:
                cur.new = w
            cur.mode_only = False
            i += 2
            continue
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if not m:
                raise PatchError("bad hunk header")
            os_, ol = int(m.group(1)), int(m.group(2) or 1)
            ns, nl = int(m.group(3)), int(m.group(4) or 1)
            body = []
            i += 1
            ro, rn = ol, nl
            while i < n and (ro > 0 or rn > 0):
                h = lines[i]
                tag = h[:1]
                if tag == "\\":
                    body.append(("\\", ""))
                elif tag == "-":
                    body.append(("-", h[1:]))
                    ro -= 1
                elif tag == "+":
                    body.append(("+", h[1:]))
                    rn -= 1
                elif tag == " " or tag == "":
                    body.append((" ", h[1:]))
                    ro -= 1
                    rn -= 1
                else:
                    raise PatchError("bad hunk line")
                i += 1
            if i < n and lines[i].startswith("\\"):
                body.append(("\\", ""))
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            cur.mode_only = False
            continue
        i += 1
    return files


def apply(old_text, hunks):
    """New text of a file after its hunks. `old_text` is '' for a new file."""
    old = old_text.split("\n")
    old_no_eol = not old_text.endswith("\n")
    if not old_no_eol:
        old.pop()
    out = []
    pos = 0  # index into old
    new_no_eol = old_no_eol
    for os_, ol, ns, nl, body in hunks:
        start = os_ - 1 if ol else os_
        want = [t for tag, t in body if tag in " -"]
        if old[start : start + len(want)] != want:
            start = _relocate(old, want, start, pos)
        if start < pos:
            raise PatchError("overlapping hunks")
        out.extend(old[pos:start])
        pos = start
        last = None
        for tag, t in body:
            if tag == " ":
                out.append(t)
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(t)
            elif tag == "\\":
                if last in "+ ":
                    new_no_eol = True
                elif last == "-" and pos >= len(old):
                    new_no_eol = False
                continue
            last = tag
        if pos >= len(old) and last == "+" and body[-1][0] != "\\":
            new_no_eol = False
    out.extend(old[pos:])
    if not out:
        return ""
    return "\n".join(out) + ("" if new_no_eol else "\n")


def _relocate(old, want, start, lo):
    for d in range(1, len(old) + 1):
        for s in (start - d, start + d):
            if lo <= s <= len(old) - len(want) and old[s : s + len(want)] == want:
                return s
    raise PatchError("context does not match")
