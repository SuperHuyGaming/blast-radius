"""Collect labeled mutants into a scoring set: OUT/commits, OUT/tests.json, OUT/answers.json."""
import glob, json, os, shutil, sys
root, out = sys.argv[1], sys.argv[2]
meta = json.load(open(os.path.join(root, "commits", "meta.json")))
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
shutil.copy("kit_x/sample/tests.json", os.path.join(out, "tests.json"))
BAD = {"7ldn"}  # a pytest-xdist worker crashed while labeling: not a real failure
if os.path.exists(os.path.join(root, "bad.txt")):
    BAD |= set(open(os.path.join(root, "bad.txt")).read().split())  # runs where a worker crashed
ans = {}
for f in sorted(glob.glob(os.path.join(root, "res", "*.json"))):
    r = json.load(open(f)); cid = r["id"]
    if r.get("status") != "ok" or r.get("missing"):
        continue
    if cid in BAD or any("killed=True" in n for n in r.get("notes", [])):
        continue  # the labeling run hung and was killed: what did not report is unknown
    src = os.path.join(root, "commits", cid + ".patch")
    if not os.path.exists(src):
        continue
    shutil.copy(src, os.path.join(out, "commits", cid + ".patch"))
    m = meta.get(cid, {})
    ans[cid] = dict(failing=sorted(r["failing"]), kind=m.get("kind", "?"), subkind=m.get("subkind", "?"), desc=m.get("desc", ""))
json.dump({"commits": ans}, open(os.path.join(out, "answers.json"), "w"))
print(len(ans), "commits")
