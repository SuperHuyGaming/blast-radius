import sys, os, subprocess, tempfile, shutil, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import blastsel
src = sys.argv[1]
bad = 0
for p in sorted(glob.glob(os.path.join(sys.argv[2], "*.patch"))):
    text = open(p).read()
    for fp in blastsel.parse_patch(text):
        old = open(os.path.join(src, fp.old_path)).read() if fp.old_path else ""
        new, ch, ins = blastsel.apply_hunks(old, fp.hunks)
        # reference via git apply on a temp copy of that one file
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, os.path.dirname(fp.path)), exist_ok=True)
        if fp.old_path:
            shutil.copy(os.path.join(src, fp.old_path), os.path.join(d, fp.old_path))
        r = subprocess.run(["git", "apply", "--include", fp.path, os.path.abspath(p)], cwd=d, capture_output=True, text=True)
        ref = open(os.path.join(d, fp.new_path)).read() if fp.new_path else None
        ok = r.returncode == 0 and ref == new
        if not ok:
            bad += 1
            print("MISMATCH", p, fp.path, r.stderr[:200])
        shutil.rmtree(d)
print("bad", bad)
