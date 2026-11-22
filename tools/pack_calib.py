"""calib json from tools/calibrate.py -> maps/calib.bin"""
import json, marshal, sys
c = json.load(open(sys.argv[1]))
bad = set(c["failing"])
marshal.dump(dict(test_cpu={k: v for k, v in c["test_cpu"].items() if k not in bad},
                  class_cpu=c["class_cpu"]), open(sys.argv[2], "wb"))
