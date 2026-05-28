# -*- coding: utf-8 -*-
# Sanity-test the new preflight helpers / whitelist parsing.
# Self-contained: exec only the new helper section to avoid importing
# the full QGIS-dependent helper module.
import os, sys, re, ast

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "SpatialAnalysisAgent")
HELPER = os.path.join(PLUGIN, "SpatialAnalysisAgent_helper.py")

with open(HELPER, "r", encoding="utf-8") as f:
    src = f.read()

# Slice from start of the new section to just before
# `_PREFLIGHT_BAD_ALGORITHM_IDS = {` (first body block we want to keep).
# Then also slice the two new preflight functions (param keys + paths).
start_marker = "# Cache: alg_id -> {'required'"
end_marker = "_PREFLIGHT_BAD_ALGORITHM_IDS = {"
i0 = src.index(start_marker)
i1 = src.index(end_marker)
helper_block_a = src[i0:i1]

# Find the two new functions
def slice_function(src, def_line):
    i = src.index("def " + def_line)
    # find next top-level def / class / # ===
    rest = src[i:]
    m = re.search(r"\n(?:def |class |# ===)", rest[20:])
    end = (m.start() + 20) if m else len(rest)
    return rest[:end]
fn_check_param_keys = slice_function(src, "_preflight_check_param_keys(alg, params)")
fn_check_param_paths = slice_function(src, "_preflight_check_param_paths(alg, params)")

ns = {"__name__": "_test_ns", "os": os, "re": re, "ast": ast, "sys": sys}
exec(helper_block_a, ns)
exec(fn_check_param_keys, ns)
exec(fn_check_param_paths, ns)

# Bind names locally for clarity
get_tool_param_whitelist     = ns["get_tool_param_whitelist"]
build_parameter_whitelist_block = ns["build_parameter_whitelist_block"]
_preflight_check_param_keys  = ns["_preflight_check_param_keys"]
_preflight_check_param_paths = ns["_preflight_check_param_paths"]

ok = 0
fail = 0
def expect(label, cond, detail=""):
    global ok, fail
    if cond:
        print(f"  PASS  {label}")
        ok += 1
    else:
        print(f"  FAIL  {label}  {detail}")
        fail += 1

# patch: get_tool_param_whitelist uses __file__ to locate Tools_Documentation,
# but the exec'd block has no __file__. Override with absolute lookup.
def _find_tool_toml_path(alg_id):
    docs_dir = os.path.join(PLUGIN, "Tools_Documentation")
    cands = [alg_id]
    if alg_id.startswith("native:"):
        cands.append("qgis:" + alg_id[len("native:"):])
    elif alg_id.startswith("qgis:"):
        cands.append("native:" + alg_id[len("qgis:"):])
    for c in cands:
        stfid = re.sub(r"[ :?\/]", "_", c)
        target = stfid + ".toml"
        for root, _d, files in os.walk(docs_dir):
            if target in files:
                return os.path.join(root, target)
    return None
ns["_find_tool_toml_path"] = _find_tool_toml_path
ns["_TOOL_PARAM_CACHE"].clear()

print("=" * 60)
print("Test 1: parameter whitelist parsed for native:lineintersections")
print("=" * 60)
wl = get_tool_param_whitelist("native:lineintersections")
print("  whitelist:", wl)
expect("alias native:->qgis: resolves", wl is not None)
if wl:
    expect("INPUT in valid params", "INPUT" in wl["all"])
    expect("INTERSECT in valid params", "INTERSECT" in wl["all"])
    expect("OUTPUT in valid params", "OUTPUT" in wl["all"])
    expect("OVERLAY NOT in valid params", "OVERLAY" not in wl["all"])

print()
print("=" * 60)
print("Test 2: preflight detects OVERLAY (case 5)")
print("=" * 60)
issues = _preflight_check_param_keys("native:lineintersections",
    {"INPUT": "<r>", "OVERLAY": "<r>", "OUTPUT": "out.shp"})
print("  issues:", issues)
expect("ERROR_CODE_PARAM_UNKNOWN raised",
       bool(issues) and issues[0][0] == "ERROR_CODE_PARAM_UNKNOWN")
expect("OVERLAY mentioned in message",
       bool(issues) and "OVERLAY" in issues[0][1])

print()
print("=" * 60)
print("Test 3: correct INTERSECT keys -> no issues")
print("=" * 60)
issues = _preflight_check_param_keys("native:lineintersections",
    {"INPUT": "<r>", "INTERSECT": "<r>", "OUTPUT": "out.shp"})
print("  issues:", issues)
expect("no false positive for valid keys", issues == [])

print()
print("=" * 60)
print("Test 4: prompt whitelist block contains INTERSECT note")
print("=" * 60)
block = build_parameter_whitelist_block(["native:lineintersections", "gdal:viewshed"])
print(block)
expect("header present", "STRICT PARAMETER NAMES" in block)
expect("INTERSECT mentioned", "INTERSECT" in block)
expect("OVERLAY warning present", "OVERLAY" in block)
expect("viewshed note present", "OBSERVER" in block)

print()
print("=" * 60)
print("Test 5: path-not-found detection (case 44)")
print("=" * 60)
fake = r"D:\nope\Laramie\Laramie.shp"
issues = _preflight_check_param_paths("native:rasterlayerzonalstats",
    {"INPUT": fake, "OUTPUT": "ok.shp"})
print("  issues:", issues)
expect("ERROR_CODE_PATH_NOT_FOUND raised",
       bool(issues) and issues[0][0] == "ERROR_CODE_PATH_NOT_FOUND")
expect("missing file path in message",
       bool(issues) and "Laramie.shp" in issues[0][1])

issues2 = _preflight_check_param_paths("any",
    {"OUTPUT": r"D:\nope\does_not_exist.shp"})
print("  issues2 (OUTPUT only):", issues2)
expect("OUTPUT excluded from path check", issues2 == [])

# A non-pathy string (just a layer name) should not trigger path check
issues3 = _preflight_check_param_paths("any",
    {"INPUT": "Major Roads"})
print("  issues3 (layer-name string):", issues3)
expect("bare display-name string ignored", issues3 == [])

print()
print("=" * 60)
print(f"Summary: PASS={ok}  FAIL={fail}")
print("=" * 60)
sys.exit(0 if fail == 0 else 1)
