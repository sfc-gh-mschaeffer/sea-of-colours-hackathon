import json
import sys

tmp, out, seed, p1, p2 = sys.argv[1:6]
text = open(tmp, "r", errors="replace").read()
start = text.find("{")
obj = None
if start != -1:
    # The JSON output nests objects ("seats", "scores"), so a naive
    # parse from the first "{" can pick up trailing bytes after the
    # outer object closes. Decode incrementally and take the first
    # complete top-level object.
    decoder = json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(text, start)
    except Exception:
        obj = None
with open(out, "a") as f:
    if obj is not None:
        obj["_seed"] = int(seed)
        obj["_p1_agent"] = p1
        obj["_p2_agent"] = p2
        f.write(json.dumps(obj) + "\n")
    else:
        f.write(json.dumps({
            "_seed": int(seed), "_p1_agent": p1, "_p2_agent": p2,
            "_error": "no_json_parsed",
        }) + "\n")
