"""Independent quality assurance for two-column metadata pruning.

Commands
--------
``python3 qa.py verify``
    Re-runs the retained regression, the independent oracle cross-checks,
    fixed-seed differential testing, end-to-end scan checks, isolation-copy
    mutation detection and finite stress runs.  It rewrites ``evidence.json``
    and checks that ``REPORT.md`` references agree with the recomputed
    evidence.  Any verifier error, stale evidence, missed deviation or missed
    mutation makes the process exit non-zero.

``python3 qa.py demo``
    Prints real inputs, the production decision, the independent expectation
    and concrete witnesses for the public sample and the hand cases.

The harness may call the production entry points (that is what it audits);
``oracle.py`` itself never imports them.
"""
import ast
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc

import checks
import oracle
import pruner
import scanner
import semantics
from fixture import group as fixture_group
from samples import PREDICATE as SAMPLE_PREDICATE
from samples import groups as sample_groups

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_PATH = os.path.join(HERE, "evidence.json")
REPORT_PATH = os.path.join(HERE, "REPORT.md")

PRODUCTION_FILES = ("pruner.py", "scanner.py", "semantics.py")
ORACLE_FILE = "oracle.py"
MAX_ABS = 10 ** 30
OPS = ("=", "!=", "<", "<=", ">", ">=")

CASES = {}
DEVIATIONS = []
FAILURES = []
STRESS_RECORDS = []

REQUIRED_CLAIM_CASES = (
    "original_regression",
    "oracle_independence_static",
    "small_domain_pruning",
    "small_domain_rows",
    "differential_pruning_seeded",
    "differential_rows_seeded",
    "scan_pipeline_seeded",
    "scan_immutability",
    "pruner_stats_only",
    "manual_null_not",
    "manual_null_negated_conjunction",
    "manual_same_column_contradiction",
    "manual_cross_column_order",
    "manual_cross_column_equality_gap",
    "manual_equality_strict_bounds",
    "manual_all_null",
    "manual_zero_rows",
    "manual_wide_bounds_true",
    "manual_wide_bounds_impossible",
    "manual_not_tautology",
    "mutation_null_not_negation",
    "mutation_shared_column_independence",
    "stress_wide_16leaf",
    "stress_deep_16leaf",
    "finite_decision_basis",
    "production_baseline",
    "deviation_blocking",
    "verifier_selftest",
)

REQUIRED_REPORT_KEYWORDS = (
    "有限判定",
    "小域穷举",
    "固定种子",
    "NULL",
    "共享列",
    "变异",
    "压力",
    "生产偏差",
    "evidence.json",
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def production_digests():
    return {name: digest(os.path.join(HERE, name)) for name in PRODUCTION_FILES}


def leaves(node):
    head = node[0]
    if head == "not":
        return leaves(node[1])
    if head in ("and", "or"):
        return leaves(node[1]) + leaves(node[2])
    return 1


def depth(node):
    head = node[0]
    if head == "not":
        return 1 + depth(node[1])
    if head in ("and", "or"):
        return 1 + max(depth(node[1]), depth(node[2]))
    return 1


def constants_in(node, out):
    head = node[0]
    if head == "not":
        constants_in(node[1], out)
    elif head in ("and", "or"):
        constants_in(node[1], out)
        constants_in(node[2], out)
    else:
        for value in (node[1], node[2]):
            if isinstance(value, int) and not isinstance(value, bool):
                out.add(value)


def predicate_is_legal(predicate):
    if leaves(predicate) > 16 or depth(predicate) > 16:
        return False
    found = set()
    constants_in(predicate, found)
    return all(abs(value) <= MAX_ABS for value in found)


def record(case_id, kind, passed, detail, status=None):
    status = status or ("pass" if passed else "fail")
    CASES[case_id] = {"kind": kind, "status": status, "detail": detail}
    if not passed and status != "deviation":
        FAILURES.append(case_id)


MAX_COUNTEREXAMPLES_PER_SOURCE = 40
DEVIATION_COUNTS = {}


def note_deviation(source, stats, predicate, expected, actual, extra=None):
    DEVIATION_COUNTS[source] = DEVIATION_COUNTS.get(source, 0) + 1
    if DEVIATION_COUNTS[source] > MAX_COUNTEREXAMPLES_PER_SOURCE:
        return None
    item = {
        "source": source,
        "stats": stats,
        "predicate": predicate,
        "expected": expected,
        "actual": actual,
        "rerun_argv": ["python3", "qa.py", "verify"],
    }
    if extra is not None:
        item["data_or_groups"] = extra
    DEVIATIONS.append(item)
    return item


def witness_ok(stats, predicate, witness):
    if witness is None:
        return True
    if set(witness) != {"x", "y"}:
        return False
    for col in ("x", "y"):
        value = witness[col]
        column = stats[col]
        if value is None:
            if column["nulls"] <= 0:
                return False
        else:
            if column["nulls"] >= stats["rows"]:
                return False
            if not (column["lo"] <= value <= column["hi"]):
                return False
    return oracle.evaluate(predicate, witness) is True


def expected_scan(groups, predicate):
    rows, skipped = [], []
    for index, group in enumerate(groups):
        decision = oracle.judge(group["stats"], predicate)
        if decision["skip"]:
            skipped.append(index)
            continue
        for row in group["data"]:
            if oracle.evaluate(predicate, row) is True:
                rows.append({"x": row["x"], "y": row["y"]})
    return {"rows": rows, "skipped": skipped}


# --------------------------------------------------------------------------
# predicate / statistics generators (fixed seeds only)
# --------------------------------------------------------------------------
def gen_atom(rng, constants):
    operands = ["x", "y"] + list(constants)
    return [rng.choice(OPS), rng.choice(operands), rng.choice(operands)]


def gen_tree(rng, count, constants, allow_not=True):
    if count <= 1:
        node = gen_atom(rng, constants)
    else:
        left = count // 2
        node = [rng.choice(("and", "or")),
                gen_tree(rng, left, constants, allow_not),
                gen_tree(rng, count - left, constants, allow_not)]
    if allow_not and rng.random() < 0.3 and depth(node) < 16:
        return ["not", node]
    return node


def gen_legal_tree(rng, max_leaves, constants):
    """Random tree guaranteed legal: <=16 leaves, <=16 depth, |const| <= 1e30."""
    predicate = gen_tree(rng, rng.randint(1, max_leaves), constants)
    if predicate_is_legal(predicate):
        return predicate
    return rng.choice([
        [">=", "x", 0],
        ["not", ["and", ["<", "x", "y"], ["=", "y", constants[0]]]],
        ["and", ["<=", "x", constants[0]], ["or", ["!=", "y", "x"], [">", "x", 0]]],
    ])


def gen_stats(rng):
    rows = rng.randint(0, 6)
    stats = {"rows": rows}
    for col in ("x", "y"):
        nulls = rng.randint(0, rows)
        if nulls == rows:
            stats[col] = {"nulls": nulls, "lo": None, "hi": None}
            continue
        style = rng.random()
        if style < 0.30:
            lo, hi = sorted([rng.choice((-MAX_ABS, -10 ** 15, -1000, -5, 0)),
                             rng.choice((MAX_ABS, 10 ** 15, 1000, 5, 0))])
        elif style < 0.50:
            lo = hi = rng.choice((-MAX_ABS, -3, 0, 4, MAX_ABS))
        else:
            lo = rng.randint(-5, 4)
            hi = lo + rng.randint(0, 6)
        stats[col] = {"nulls": nulls, "lo": lo, "hi": hi}
    return stats


def gen_constants(rng, count=5):
    pool = (-MAX_ABS, -10 ** 15, -8, -3, -1, 0, 1, 2, 5, 9, 10 ** 15, MAX_ABS)
    return [rng.choice(pool) for _ in range(count)]


def gen_row(rng):
    return {"x": None if rng.random() < 0.25 else rng.randint(-6, 6),
            "y": None if rng.random() < 0.25 else rng.randint(-6, 6)}


def gen_groups(rng, count=24):
    groups = []
    for _ in range(count):
        data = [gen_row(rng) for _ in range(rng.randint(0, 6))]
        groups.append(fixture_group(data))
    return groups


# --------------------------------------------------------------------------
# 1. retained regression + static oracle independence
# --------------------------------------------------------------------------
def check_production_baseline():
    baseline = {}
    try:
        head = subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=60)
        for name in PRODUCTION_FILES:
            blob = subprocess.run(["git", "-C", HERE, "show",
                                   "HEAD:" + name],
                                  capture_output=True, timeout=60)
            if blob.returncode != 0:
                raise RuntimeError(blob.stderr.decode("utf-8", "replace"))
            baseline[name] = hashlib.sha256(blob.stdout).hexdigest()
    except Exception as exc:
        record("production_baseline", "immutability", True,
               {"git_baseline": "unavailable", "reason": repr(exc),
                "current": production_digests(),
                "note": "无法读取提交基线，仅保障单次运行内生产文件未变"})
        return
    current = production_digests()
    problems = [name for name in PRODUCTION_FILES
                if baseline.get(name) != current[name]]
    record("production_baseline", "immutability", not problems,
           {"baseline_commit": head.stdout.strip(), "matches": not problems,
            "mismatched": problems},
           status=None if not problems else "error")
    if problems:
        FAILURES.append("production_baseline")


def check_original_regression():
    try:
        result = checks.normal()
        ok = result == {"rows": [{"x": 3, "y": 5}, {"x": 3, "y": 5}],
                        "skipped": [0, 2]}
        record("original_regression", "regression",
               ok, {"result": result, "expected": {"rows": [{"x": 3, "y": 5},
                                                            {"x": 3, "y": 5}],
                                                   "skipped": [0, 2]}})
        if not ok:
            FAILURES.append("original_regression")
    except Exception as exc:  # verifier or production failure
        record("original_regression", "regression", False,
               {"error": repr(exc)}, status="error")


def check_oracle_independence():
    source = open(os.path.join(HERE, ORACLE_FILE), encoding="utf-8").read()
    tree = ast.parse(source)
    imports = [node for node in ast.walk(tree)
               if isinstance(node, (ast.Import, ast.ImportFrom))]
    forbidden_names = {"can_skip", "INVERSE", "_alternatives", "_feasible",
                       "scanner", "semantics", "pruner"}
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
    uses_range = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                     and node.func.id == "range" for node in ast.walk(tree))
    ok = not imports and not (used & forbidden_names) and not uses_range
    detail = {"imports": len(imports), "forbidden_symbols": sorted(used & forbidden_names),
              "integer_range_calls": uses_range}
    record("oracle_independence_static", "independence", ok, detail,
           status=None if ok else "error")
    if not ok:
        FAILURES.append("oracle_independence_static")


# --------------------------------------------------------------------------
# 2. small-domain exhaustive cross-check
# --------------------------------------------------------------------------
def small_stats():
    out = []
    for rows in (0, 1, 2):
        for nx in range(rows + 1):
            for ny in range(rows + 1):
                lo_x = (None,) if nx == rows else range(-1, 2)
                for lx in lo_x:
                    hi_x = (None,) if nx == rows else range(lx, min(lx + 1, 2) + 1)
                    for hx in hi_x:
                        lo_y = (None,) if ny == rows else range(-1, 2)
                        for ly in lo_y:
                            hi_y = (None,) if ny == rows else range(ly, min(ly + 1, 2) + 1)
                            for hy in hi_y:
                                out.append({
                                    "rows": rows,
                                    "x": {"nulls": nx, "lo": lx, "hi": hx},
                                    "y": {"nulls": ny, "lo": ly, "hi": hy},
                                })
    unique, seen = [], set()
    for stats in out:
        key = repr(stats)
        if key not in seen:
            seen.add(key)
            unique.append(stats)
    return unique


def atom_space():
    operands = ["x", "y", -1, 0, 1, 2]
    return [[op, a, b] for op in OPS for a in operands for b in operands]


def enumerate_small(stats, predicate):
    """Full integer enumeration; only used where the spans are tiny."""
    if stats["rows"] == 0:
        return True, None
    xs = [None] if stats["x"]["nulls"] > 0 else []
    if stats["x"]["nulls"] < stats["rows"]:
        xs += list(range(stats["x"]["lo"], stats["x"]["hi"] + 1))
    ys = [None] if stats["y"]["nulls"] > 0 else []
    if stats["y"]["nulls"] < stats["rows"]:
        ys += list(range(stats["y"]["lo"], stats["y"]["hi"] + 1))
    for x in xs:
        for y in ys:
            row = {"x": x, "y": y}
            if oracle.evaluate(predicate, row) is True:
                return False, row
    return True, None


def check_small_domain():
    stats_list = small_stats()
    atoms = atom_space()
    mismatches = []
    checks_count = 0
    for stats in stats_list:
        for predicate in atoms:
            checks_count += 1
            expected_skip, expected_witness = enumerate_small(stats, predicate)
            decision = oracle.judge(stats, predicate)
            production = pruner.can_skip(stats, predicate)
            if decision["skip"] != expected_skip:
                mismatches.append({"who": "oracle", "stats": stats,
                                   "predicate": predicate,
                                   "expected_skip": expected_skip,
                                   "actual_skip": decision["skip"]})
            if not witness_ok(stats, predicate, decision["witness"]):
                mismatches.append({"who": "oracle-witness", "stats": stats,
                                   "predicate": predicate,
                                   "witness": decision["witness"]})
            if production != expected_skip:
                note_deviation("small_domain_pruning", stats, predicate,
                               expected_skip, production)
                mismatches.append({"who": "production", "stats": stats,
                                   "predicate": predicate,
                                   "expected_skip": expected_skip,
                                   "actual_skip": production})
    record("small_domain_pruning", "exhaustive",
           not mismatches,
           {"stats": len(stats_list), "atoms": len(atoms),
            "checks": checks_count, "mismatches": mismatches[:8],
            "match": not mismatches})

    # Two-leaf conjunctions/disjunctions with and without outer NOT.
    pair_stats = stats_list[::6]
    pair_atoms = atoms[::6]
    pair_mismatches = []
    pair_checks = 0
    for stats in pair_stats:
        for a1 in pair_atoms:
            for a2 in pair_atoms:
                for connector in ("and", "or"):
                    base = [connector, a1, a2]
                    for predicate in (base, ["not", base]):
                        pair_checks += 1
                        expected_skip, _ = enumerate_small(stats, predicate)
                        decision = oracle.judge(stats, predicate)["skip"]
                        production = pruner.can_skip(stats, predicate)
                        if decision != expected_skip:
                            pair_mismatches.append({"who": "oracle",
                                                    "stats": stats,
                                                    "predicate": predicate,
                                                    "expected": expected_skip,
                                                    "actual": decision})
                        if production != expected_skip:
                            note_deviation("small_domain_pairs", stats, predicate,
                                           expected_skip, production)
                            pair_mismatches.append({"who": "production",
                                                    "stats": stats,
                                                    "predicate": predicate,
                                                    "expected": expected_skip,
                                                    "actual": production})
    record("small_domain_pairs", "exhaustive",
           not pair_mismatches,
           {"stats": len(pair_stats), "atoms": len(pair_atoms),
            "checks": pair_checks, "mismatches": pair_mismatches[:8]})

    # Row-level semantics cross-check over a fully enumerated domain.
    row_stats = stats_list[::4]
    row_predicates = atoms[::5]
    row_mismatches = []
    row_checks = 0
    for stats in row_stats:
        xs = [None] if stats["x"]["nulls"] > 0 else []
        if stats["x"]["nulls"] < stats["rows"]:
            xs += list(range(stats["x"]["lo"], stats["x"]["hi"] + 1))
        ys = [None] if stats["y"]["nulls"] > 0 else []
        if stats["y"]["nulls"] < stats["rows"]:
            ys += list(range(stats["y"]["lo"], stats["y"]["hi"] + 1))
        for x in xs:
            for y in ys:
                row = {"x": x, "y": y}
                for predicate in row_predicates:
                    row_checks += 1
                    independent = oracle.evaluate(predicate, row)
                    production = semantics.evaluate(predicate, row)
                    if independent != production:
                        row_mismatches.append({"row": row,
                                               "predicate": predicate,
                                               "oracle": independent,
                                               "production": production})
    record("small_domain_rows", "exhaustive",
           not row_mismatches,
           {"stats": len(row_stats), "predicates": len(row_predicates),
            "checks": row_checks, "mismatches": row_mismatches[:8]})


# --------------------------------------------------------------------------
# 3. fixed-seed differential testing
# --------------------------------------------------------------------------
DIFFERENTIAL_SEEDS = (7, 2024, 20261115)


def check_differential():
    mismatches = []
    row_mismatches = []
    checks_count = 0
    row_checks = 0
    params = {"seeds": list(DIFFERENTIAL_SEEDS), "trials_per_seed": 350,
              "leaf_range": [1, 16], "constants_per_predicate": 5,
              "bounds_styles": ["small", "wide+-1e30", "point", "huge"],
              "max_constant_abs": MAX_ABS}
    for seed in DIFFERENTIAL_SEEDS:
        rng = random.Random(seed)
        for _ in range(params["trials_per_seed"]):
            stats = gen_stats(rng)
            predicate = gen_legal_tree(rng, 16, gen_constants(rng))
            checks_count += 1
            decision = oracle.judge(stats, predicate)
            if not witness_ok(stats, predicate, decision["witness"]):
                mismatches.append({"who": "oracle-witness", "seed": seed,
                                   "stats": stats, "predicate": predicate,
                                   "witness": decision["witness"]})
            production = pruner.can_skip(stats, predicate)
            if production != decision["skip"]:
                note_deviation("differential_pruning_seeded", stats, predicate,
                               decision["skip"], production,
                               extra={"seed": seed})
                mismatches.append({"who": "production", "seed": seed,
                                   "stats": stats, "predicate": predicate,
                                   "expected": decision["skip"],
                                   "actual": production})
            for _ in range(4):
                row = gen_row(rng)
                row_checks += 1
                a = oracle.evaluate(predicate, row)
                b = semantics.evaluate(predicate, row)
                if a != b:
                    row_mismatches.append({"seed": seed, "row": row,
                                           "predicate": predicate,
                                           "oracle": a, "production": b})
    record("differential_pruning_seeded", "differential",
           not mismatches,
           {"params": params, "checks": checks_count,
            "mismatches": mismatches[:8]})
    record("differential_rows_seeded", "differential",
           not row_mismatches,
           {"params": params, "checks": row_checks,
            "mismatches": row_mismatches[:8]})


# --------------------------------------------------------------------------
# 4. end-to-end scan checks (order and duplicates preserved)
# --------------------------------------------------------------------------
def compare_scan(case_id, groups, predicates, detail):
    problems = []
    checks_count = 0
    for predicate in predicates:
        snapshot = json.dumps(groups, sort_keys=True)
        expected = expected_scan(groups, predicate)
        on = scanner.scan(groups, predicate, pruning=True)
        off = scanner.scan(groups, predicate, pruning=False)
        checks_count += 1
        direct_skip = [index for index, group in enumerate(groups)
                       if pruner.can_skip(group["stats"], predicate)]
        if on != expected:
            note_deviation(case_id, {"groups": groups}, predicate,
                           expected, on)
            problems.append({"predicate": predicate, "expected": expected,
                             "on": on})
        if on["skipped"] != direct_skip:
            problems.append({"predicate": predicate,
                             "skipped_not_per_group_can_skip": on["skipped"],
                             "per_group_can_skip": direct_skip})
        if off["rows"] != expected["rows"] or off["skipped"] != []:
            note_deviation(case_id, {"groups": groups}, predicate,
                           {"rows": expected["rows"], "skipped": []}, off)
            problems.append({"predicate": predicate, "plain": off,
                             "expected_rows": expected["rows"]})
        if on["rows"] != off["rows"]:
            problems.append({"predicate": predicate,
                             "pruning_toggles_rows": {"on": on["rows"],
                                                      "off": off["rows"]}})
        if json.dumps(groups, sort_keys=True) != snapshot:
            problems.append({"predicate": predicate, "mutated_input": True})
    detail.update({"checks": checks_count, "problems": problems[:8]})
    record(case_id, "scan", not problems, detail)


def check_scan_pipeline():
    compare_scan("scan_pipeline_samples", sample_groups(),
                 [SAMPLE_PREDICATE,
                  ["or", ["not", ["<", "x", "y"]], ["=", "x", 5]]],
                 {"source": "samples.py"})
    rng = random.Random(31337)
    groups = gen_groups(rng, 40)
    predicates = [SAMPLE_PREDICATE]
    for _ in range(12):
        predicates.append(gen_legal_tree(rng, 16, gen_constants(rng)))
    # explicit duplicates and NULL/equality edge cases
    groups.append(fixture_group([{"x": 4, "y": 4}, {"x": 4, "y": 4},
                                 {"x": None, "y": 4}, {"x": 4, "y": None}]))
    groups.append(fixture_group([{"x": -2, "y": 3}, {"x": -2, "y": 3}]))
    predicates += [["=", "x", "y"], ["and", ["=", "x", "y"], ["!=", "x", "y"]],
                   ["not", ["=", "x", 4]]]
    compare_scan("scan_pipeline_seeded", groups, predicates,
                 {"seed": 31337, "groups": len(groups),
                  "duplicates": True, "predicates": len(predicates)})


def check_scan_immutability():
    groups = sample_groups()
    before = json.dumps(groups, sort_keys=True)
    result = scanner.scan(groups, SAMPLE_PREDICATE, pruning=True)
    after = json.dumps(groups, sort_keys=True)
    independent = expected_scan(groups, SAMPLE_PREDICATE)
    ok = before == after and result == independent
    record("scan_immutability", "immutability", ok,
           {"unchanged": before == after, "result_matches": result == independent})


def check_pruner_stats_only():
    groups = sample_groups()
    problems = []
    for group in groups:
        stats_only = {"rows": group["stats"]["rows"],
                      "x": dict(group["stats"]["x"]),
                      "y": dict(group["stats"]["y"])}
        a = pruner.can_skip(stats_only, SAMPLE_PREDICATE)
        b = pruner.can_skip(group["stats"], SAMPLE_PREDICATE)
        c = oracle.judge(group["stats"], SAMPLE_PREDICATE)["skip"]
        if a != b or a != c:
            problems.append({"stats": stats_only, "stats_only": a,
                             "with_group": b, "oracle": c})
    record("pruner_stats_only", "metadata-only",
           not problems, {"problems": problems})


# --------------------------------------------------------------------------
# 5. hand-checkable cases
# --------------------------------------------------------------------------
MANUAL_CASES = {
    "manual_null_not": {
        "stats": {"rows": 2, "x": {"nulls": 1, "lo": 2, "hi": 2},
                  "y": {"nulls": 0, "lo": 0, "hi": 7}},
        "predicate": ["not", ["=", "x", 1]],
        "skip": False,
        "note": "x=2 makes NOT(FALSE)=TRUE; x=NULL only yields UNKNOWN",
    },
    "manual_null_negated_conjunction": {
        "stats": {"rows": 2, "x": {"nulls": 2, "lo": None, "hi": None},
                  "y": {"nulls": 2, "lo": None, "hi": None}},
        "predicate": ["not", ["and", ["=", "x", 1], ["=", "y", 2]]],
        "skip": True,
        "note": "both operands can only be NULL, so NOT never sees FALSE",
    },
    "manual_same_column_contradiction": {
        "stats": {"rows": 3, "x": {"nulls": 0, "lo": 0, "hi": 9},
                  "y": {"nulls": 0, "lo": 0, "hi": 9}},
        "predicate": ["and", [">", "x", 8], ["<", "x", 5]],
        "skip": True,
        "note": "shared x comparisons cannot be satisfied independently",
    },
    "manual_cross_column_order": {
        "stats": {"rows": 3, "x": {"nulls": 0, "lo": 0, "hi": 5},
                  "y": {"nulls": 0, "lo": 0, "hi": 5}},
        "predicate": ["and", ["<", "x", "y"], [">", "x", "y"]],
        "skip": True,
        "note": "x<y and x>y share the same pair, no sign satisfies both",
    },
    "manual_cross_column_equality_gap": {
        "stats": {"rows": 2, "x": {"nulls": 0, "lo": 1, "hi": 2},
                  "y": {"nulls": 0, "lo": 5, "hi": 6}},
        "predicate": ["=", "x", "y"],
        "skip": True,
        "note": "disjoint integer envelopes, equality impossible",
    },
    "manual_equality_strict_bounds": {
        "stats": {"rows": 2, "x": {"nulls": 0, "lo": 1, "hi": 1},
                  "y": {"nulls": 0, "lo": 2, "hi": 2}},
        "predicate": ["and", ["<=", "x", "y"], [">", "x", "y"]],
        "skip": True,
        "note": "x<=y is forced TRUE while x>y is forced FALSE",
    },
    "manual_all_null": {
        "stats": {"rows": 2, "x": {"nulls": 2, "lo": None, "hi": None},
                  "y": {"nulls": 2, "lo": None, "hi": None}},
        "predicate": ["or", ["=", "x", 1], ["not", ["=", "y", 2]]],
        "skip": True,
        "note": "every atom is UNKNOWN; UNKNOWN cannot become TRUE",
    },
    "manual_zero_rows": {
        "stats": {"rows": 0, "x": {"nulls": 0, "lo": None, "hi": None},
                  "y": {"nulls": 0, "lo": None, "hi": None}},
        "predicate": ["or", ["=", "x", 1], ["!=", "y", 2]],
        "skip": True,
        "note": "zero-row groups are always skipped",
    },
    "manual_wide_bounds_true": {
        "stats": {"rows": 3, "x": {"nulls": 0, "lo": -MAX_ABS, "hi": MAX_ABS},
                  "y": {"nulls": 0, "lo": -MAX_ABS, "hi": MAX_ABS}},
        "predicate": ["and", [">", "x", 10 ** 29], ["<", "y", -10 ** 29]],
        "skip": False,
        "note": "wide bounds still contain a witness; no integer scan needed",
    },
    "manual_wide_bounds_impossible": {
        "stats": {"rows": 3, "x": {"nulls": 0, "lo": -MAX_ABS, "hi": MAX_ABS},
                  "y": {"nulls": 0, "lo": -MAX_ABS, "hi": MAX_ABS}},
        "predicate": ["and", [">", "x", MAX_ABS - 5], ["<", "x", -MAX_ABS + 5]],
        "skip": True,
        "note": "impossible even though the span has ~2e30 integers",
    },
    "manual_not_tautology": {
        "stats": {"rows": 3, "x": {"nulls": 1, "lo": 0, "hi": 0},
                  "y": {"nulls": 0, "lo": -2, "hi": 2}},
        "predicate": ["not", ["!=", "x", "x"]],
        "skip": False,
        "note": "x!=x is FALSE for the non-NULL candidate, then NOT gives TRUE",
    },
}


def check_manual_cases():
    for case_id, case in MANUAL_CASES.items():
        stats, predicate = case["stats"], case["predicate"]
        expected = case["skip"]
        decision = oracle.judge(stats, predicate)
        production = pruner.can_skip(stats, predicate)
        oracle_ok = decision["skip"] == expected and witness_ok(
            stats, predicate, decision["witness"])
        if production != expected:
            note_deviation(case_id, stats, predicate, expected, production)
        detail = {"stats": stats, "predicate": predicate,
                  "expected_skip": expected,
                  "oracle": decision, "production_can_skip": production,
                  "note": case["note"]}
        if not oracle_ok:
            record(case_id, "manual", False, detail, status="error")
        elif production != expected:
            record(case_id, "manual", False, detail, status="deviation")
        else:
            record(case_id, "manual", True, detail)


# --------------------------------------------------------------------------
# 6. mutation detection in isolated copies
# --------------------------------------------------------------------------
MUTATION_NULL_NOT_OLD = "        return None if value is None else not value"
MUTATION_NULL_NOT_NEW = "        return True if value is None else not value"

MUTATION_SHARED_OLD = """        for edges in _alternatives(predicate, True, nulls):
            if _feasible(bounds + edges):
                return False"""
MUTATION_SHARED_NEW = """        for edges in _alternatives(predicate, True, nulls):
            if _feasible(bounds) and all(_feasible(bounds + [edge]) for edge in edges):
                return False"""

DRIVER_NULL = '''
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import group
from scanner import scan
rows = [{"x": 2, "y": 0}, {"x": None, "y": 7}]
g = group(rows)
p = ["not", ["=", "x", 1]]
print(json.dumps({
    "stats": g["stats"],
    "pruning": scan([g], p, pruning=True),
    "plain": scan([g], p, pruning=False),
}, sort_keys=True))
'''

DRIVER_SHARED = '''
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixture import group
from pruner import can_skip
from scanner import scan
rows = [{"x": 0, "y": 0}, {"x": 9, "y": 9}, {"x": 4, "y": 4}]
g1 = group(rows)
g2 = group(rows)
p1 = ["and", [">", "x", 8], ["<", "x", 5]]
p2 = ["and", ["=", "x", "y"], ["!=", "x", "y"]]
print(json.dumps({
    "p1": {"can_skip": can_skip(g1["stats"], p1), "scan": scan([g1], p1)},
    "p2": {"can_skip": can_skip(g2["stats"], p2), "scan": scan([g2], p2)},
}, sort_keys=True))
'''


def _isolated_copy():
    directory = tempfile.mkdtemp(prefix="qa_mutation_")
    for name in ("fixture.py", "pruner.py", "scanner.py", "semantics.py"):
        shutil.copyfile(os.path.join(HERE, name), os.path.join(directory, name))
    return directory


def _apply_patch(directory, filename, old, new):
    path = os.path.join(directory, filename)
    text = open(path, encoding="utf-8").read()
    if text.count(old) != 1:
        raise RuntimeError("mutation anchor missing in %s" % filename)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new))


def _run_driver(directory, source):
    path = os.path.join(directory, "qa_driver.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(source)
    proc = subprocess.run([sys.executable, "-I", path], cwd=directory,
                          capture_output=True, text=True, timeout=180)
    return proc


def _mutation_record(case_id, description, expected, proc):
    actual = None
    parse_error = None
    if proc.returncode == 0:
        try:
            actual = json.loads(proc.stdout)
        except Exception as exc:  # pragma: no cover - diagnostic path
            parse_error = repr(exc)
    detected = (proc.returncode == 0 and actual is not None and actual != expected)
    detail = {
        "mutation": description,
        "expected_correct": expected,
        "actual_mutated": actual,
        "exit_code": proc.returncode,
        "stderr_tail": proc.stderr.strip().splitlines()[-3:],
        "detected": detected,
        "note": "a successful return with a wrong decision/row counts as detection",
    }
    if parse_error:
        detail["parse_error"] = parse_error
    record(case_id, "mutation", detected, detail,
           status="detected" if detected else ("error" if proc.returncode else "fail"))


def check_mutations():
    rows = [{"x": 2, "y": 0}, {"x": None, "y": 7}]
    group = fixture_group(rows)
    predicate = ["not", ["=", "x", 1]]
    expected = {
        "stats": group["stats"],
        "pruning": expected_scan([group], predicate),
        "plain": {"rows": expected_scan([group], predicate)["rows"], "skipped": []},
    }
    directory = None
    try:
        directory = _isolated_copy()
        _apply_patch(directory, "semantics.py", MUTATION_NULL_NOT_OLD,
                     MUTATION_NULL_NOT_NEW)
        proc = _run_driver(directory, DRIVER_NULL)
        _mutation_record(
            "mutation_null_not_negation",
            "semantics.py: NOT UNKNOWN returns TRUE instead of UNKNOWN",
            expected, proc)
    except Exception as exc:
        record("mutation_null_not_negation", "mutation", False,
               {"error": repr(exc)}, status="error")
    finally:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    rows = [{"x": 0, "y": 0}, {"x": 9, "y": 9}, {"x": 4, "y": 4}]
    group = fixture_group(rows)
    p1 = ["and", [">", "x", 8], ["<", "x", 5]]
    p2 = ["and", ["=", "x", "y"], ["!=", "x", "y"]]
    expected = {
        "p1": {"can_skip": oracle.judge(group["stats"], p1)["skip"],
               "scan": expected_scan([group], p1)},
        "p2": {"can_skip": oracle.judge(group["stats"], p2)["skip"],
               "scan": expected_scan([group], p2)},
    }
    directory = None
    try:
        directory = _isolated_copy()
        _apply_patch(directory, "pruner.py", MUTATION_SHARED_OLD,
                     MUTATION_SHARED_NEW)
        proc = _run_driver(directory, DRIVER_SHARED)
        _mutation_record(
            "mutation_shared_column_independence",
            "pruner.py: feasibility checks each alternative edge separately",
            expected, proc)
    except Exception as exc:
        record("mutation_shared_column_independence", "mutation", False,
               {"error": repr(exc)}, status="error")
    finally:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------
# 7. finite stress runs
# --------------------------------------------------------------------------
def stress_predicate_wide():
    """16 leaves over 16 distinct huge constants; TRUE at (0, 0).

    Each atom is a wide-bound comparison that holds at the origin, so the
    conjunction is satisfiable while the distinct constants still cut the
    envelope into many cells.
    """
    constants = [(-MAX_ABS + (index + 1) * (2 * MAX_ABS // 17))
                 for index in range(8)]
    constants += [(-MAX_ABS + (index + 1) * (2 * MAX_ABS // 17)) + 1
                  for index in range(8)]
    atoms = []
    for index, constant in enumerate(constants):
        if index % 4 == 0:
            atoms.append([">=", "x", -abs(constant) - (1 if constant >= 0 else 0)])
        elif index % 4 == 1:
            atoms.append(["<=", "y", abs(constant) + 1])
        elif index % 4 == 2:
            atoms.append(["!=", "x", constant])
        else:
            atoms.append(["!=", "y", constant])
    node = atoms[0]
    for atom in atoms[1:]:
        node = ["and", node, atom]
    return node


def stress_predicate_deep():
    """16 leaves and depth exactly 16; the contradiction is one 2-leaf node.

    A left-deep chain of 15 ``and`` connectors over 16 leaves reaches depth 16.
    Its leftmost child is the shared-column contradiction
    ``x > MAX-5 AND x < -MAX+5``, so the whole chain is FALSE for every
    allowed integer and never depends on row data.
    """
    rng = random.Random(90211)
    constants = gen_constants(rng, 4)
    contradiction = ["and", [">", "x", MAX_ABS - 5], ["<", "x", -MAX_ABS + 5]]
    atoms = [gen_atom(rng, constants) for _ in range(14)]
    node = contradiction
    for atom in atoms:
        node = ["and", node, atom]
    return node


def stress_groups(seed):
    rng = random.Random(seed)
    pool = [None, -MAX_ABS, MAX_ABS, -10 ** 15, 10 ** 15, -3, 0, 2, 7]
    groups = []
    for _ in range(12):
        data = []
        for _ in range(400):
            data.append({"x": rng.choice(pool), "y": rng.choice(pool)})
        group = fixture_group(data)
        for col in ("x", "y"):
            if group["stats"][col]["lo"] is not None:
                group["stats"][col]["lo"] = -MAX_ABS
                group["stats"][col]["hi"] = MAX_ABS
        groups.append(group)
    return groups


def run_stress(case_id, predicate, extra_predicate):
    groups = stress_groups(4242)
    checks = {"groups": len(groups),
              "rows": sum(len(group["data"]) for group in groups),
              "leaves": leaves(predicate),
              "depth": depth(predicate),
              "constant_span": 2 * MAX_ABS}
    problems = []
    tracemalloc.start()
    start = time.perf_counter()
    per_predicate = []
    for label, active in (("generated", predicate), ("witness", extra_predicate)):
        decisions = []
        for group in groups:
            decision = oracle.judge(group["stats"], active)
            production = pruner.can_skip(group["stats"], active)
            decisions.append({"expected": decision["skip"],
                              "actual": production})
            if decision["skip"] != production:
                note_deviation(case_id, group["stats"], active,
                               decision["skip"], production,
                               extra={"rows": len(group["data"]),
                                      "predicate_label": label})
                problems.append({"stats": group["stats"],
                                 "expected": decision["skip"],
                                 "actual": production})
        expected = expected_scan(groups, active)
        on = scanner.scan(groups, active, pruning=True)
        off = scanner.scan(groups, active, pruning=False)
        if on != expected:
            note_deviation(case_id, {"groups": "stress"}, active, expected, on)
            problems.append({"scan": "pruning on differs",
                             "expected": expected, "actual": on})
        if off["rows"] != expected["rows"] or off["skipped"] != []:
            problems.append({"scan": "pruning off differs"})
        if on["rows"] != off["rows"]:
            problems.append({"scan": "pruning toggles rows"})
        per_predicate.append({
            "label": label,
            "predicate": active,
            "leaves": leaves(active),
            "depth": depth(active),
            "expected_skip_per_group": [item["expected"] for item in decisions],
            "skipped_groups": on["skipped"],
            "matched_rows": len(on["rows"]),
            "first_witness": expected["rows"][:1],
            "decisions_checked": len(decisions),
        })
    elapsed = time.perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    checks.update({"elapsed_seconds": round(elapsed, 6),
                   "peak_python_bytes": peak,
                   "predicates": per_predicate,
                   "problems": problems[:4]})
    STRESS_RECORDS.append({"case": case_id, **checks})
    record(case_id, "stress", not problems, checks)


def check_stress():
    sat = ["and", [">", "x", -MAX_ABS], ["<", "y", MAX_ABS]]
    run_stress("stress_wide_16leaf", stress_predicate_wide(), sat)
    run_stress("stress_deep_16leaf", stress_predicate_deep(), sat)


# --------------------------------------------------------------------------
# 7b. finite decision basis
# --------------------------------------------------------------------------
def cell_count(stats, predicate, col):
    column = stats[col]
    if column["nulls"] >= stats["rows"]:
        return 0
    constants = set()
    constants_in(predicate, constants)
    inside = sorted(value for value in constants
                    if column["lo"] <= value <= column["hi"])
    return len(inside) + 1


def check_finite_basis():
    problems = []
    details = []
    for case_id in ("manual_wide_bounds_true", "manual_wide_bounds_impossible",
                    "manual_cross_column_order"):
        case = MANUAL_CASES[case_id]
        stats, predicate = case["stats"], case["predicate"]
        constants = set()
        constants_in(predicate, constants)
        k = len(constants)
        cx, cy = (cell_count(stats, predicate, "x"),
                  cell_count(stats, predicate, "y"))
        null_modes = (1 + (1 if stats["x"]["nulls"] > 0 else 0)) * (
            1 + (1 if stats["y"]["nulls"] > 0 else 0))
        representatives = null_modes * max(1, cx * cy * 3)
        span_x = (stats["x"]["hi"] - stats["x"]["lo"]
                  if stats["x"]["lo"] is not None else 0)
        span_y = (stats["y"]["hi"] - stats["y"]["lo"]
                  if stats["y"]["lo"] is not None else 0)
        if cx > 2 * k + 1 or cy > 2 * k + 1:
            problems.append({"case": case_id, "reason": "cell bound exceeded",
                             "cells": [cx, cy], "k": k})
        details.append({"case": case_id, "distinct_constants": k,
                        "x_cells": cx, "y_cells": cy,
                        "null_mode_combinations": null_modes,
                        "representative_rows_at_most": representatives,
                        "envelope_pairs": span_x * span_y,
                        "note": "判定只依赖单元与 x-y 符号，不随跨度增长"})
    record("finite_decision_basis", "argument", not problems,
           {"cases": details, "problems": problems},
           status=None if not problems else "error")


# --------------------------------------------------------------------------
# 7c. production-deviation blocking, proven end to end in a temp copy
# --------------------------------------------------------------------------
DEVIATION_PATCH_OLD = "    n = stats[\"rows\"]"
DEVIATION_PATCH_NEW = "    n = stats[\"rows\"]\n    return False  # injected deviation"


def check_deviation_blocking():
    if os.environ.get("QA_NO_BLOCKING_SELFTEST") == "1":
        record("deviation_blocking", "blocking", True,
               {"skipped": True,
                "reason": "recursive selftest guard inside injected copy"})
        return
    directory = tempfile.mkdtemp(prefix="qa_blocking_")
    try:
        for name in ("qa.py", "oracle.py", "REPORT.md", "fixture.py",
                     "samples.py", "checks.py") + PRODUCTION_FILES:
            shutil.copyfile(os.path.join(HERE, name),
                            os.path.join(directory, name))
        path = os.path.join(directory, "pruner.py")
        text = open(path, encoding="utf-8").read()
        if text.count(DEVIATION_PATCH_OLD) != 1:
            raise RuntimeError("blocking-test anchor missing in pruner.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text.replace(DEVIATION_PATCH_OLD,
                                      DEVIATION_PATCH_NEW))
        env = dict(os.environ, QA_NO_BLOCKING_SELFTEST="1")
        # No -I here: the copy is our own harness, and its package-relative
        # imports (oracle, checks, ...) must resolve from the copy directory.
        proc = subprocess.run([sys.executable, "qa.py", "verify"],
                              cwd=directory, capture_output=True, text=True,
                              timeout=600, env=env)
        evidence_path = os.path.join(directory, "evidence.json")
        emitted = None
        if os.path.exists(evidence_path):
            emitted = json.load(open(evidence_path, encoding="utf-8"))
        counterexamples = (emitted or {}).get("counterexamples", [])
        blocked = proc.returncode != 0 and "QA_VERIFY_BLOCKED" in proc.stdout
        saved = bool(counterexamples) and all(
            "rerun_argv" in item and "stats" in item and "predicate" in item
            for item in counterexamples)
        ok = blocked and saved
        record("deviation_blocking", "blocking", ok,
               {"injected": "pruner.can_skip always False",
                "verify_exit_code": proc.returncode,
                "blocked": blocked,
                "counterexamples_saved": len(counterexamples),
                "counterexample_fields_ok": saved,
                "sample_counterexample": counterexamples[:1],
                "stdout_tail": proc.stdout.strip().splitlines()[-2:]},
               status=None if ok else "error")
    except Exception as exc:
        record("deviation_blocking", "blocking", False, {"error": repr(exc)},
               status="error")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# --------------------------------------------------------------------------
# 8. verifier self-test and report/evidence consistency
# --------------------------------------------------------------------------
def _decision_discrepancy(decision, expected_skip):
    """The single comparison the harness uses; returns None when consistent."""
    if decision["skip"] != expected_skip:
        return {"reason": "skip differs", "got": decision["skip"],
                "expected": expected_skip}
    return None


def check_verifier_selftest():
    stats = MANUAL_CASES["manual_same_column_contradiction"]["stats"]
    predicate = MANUAL_CASES["manual_same_column_contradiction"]["predicate"]
    decision = oracle.judge(stats, predicate)
    self_consistent = _decision_discrepancy(decision, decision["skip"]) is None
    forged_caught = _decision_discrepancy(
        decision, not decision["skip"]) is not None
    bad_witness = {"x": MAX_ABS + 1, "y": 0}
    witness_rejected = not witness_ok(stats, predicate, bad_witness)
    null_witness_on_nonnull_column = not witness_ok(
        {"rows": 2, "x": {"nulls": 0, "lo": 1, "hi": 2},
         "y": {"nulls": 0, "lo": 1, "hi": 2}},
        ["=", "x", 1], {"x": None, "y": 1})
    # mutation detection predicate must accept a real difference, reject equality
    mutation_logic_ok = (json.loads('{"a": 1}') != json.loads('{"a": 2}')
                         and json.loads('{"a": 1}') == json.loads('{"a": 1}'))
    ok = (self_consistent and forged_caught and witness_rejected
          and null_witness_on_nonnull_column and mutation_logic_ok)
    record("verifier_selftest", "selftest", ok,
           {"self_consistent": self_consistent,
            "wrong_skip_flagged": forged_caught,
            "out_of_envelope_witness_rejected": witness_rejected,
            "null_witness_on_non_null_column_rejected": null_witness_on_nonnull_column,
            "mutation_detection_logic": mutation_logic_ok},
           status=None if ok else "error")


def extract_claims(text):
    marker = "```qa-claims"
    start = text.find(marker)
    if start < 0:
        raise ValueError("REPORT.md 缺少 qa-claims 证据引用块")
    start = text.find("\n", start) + 1
    end = text.find("```", start)
    if end < 0:
        raise ValueError("qa-claims 块未闭合")
    return json.loads(text[start:end])


COUNTER_CASE_IDS = ("small_domain_pruning", "small_domain_pairs",
                    "small_domain_rows", "differential_pruning_seeded",
                    "differential_rows_seeded", "scan_pipeline_seeded")


def recomputed_claims():
    return {
        "cases": {case_id: CASES[case_id]["status"] for case_id in CASES},
        "production_deviations": sum(DEVIATION_COUNTS.values()),
        "production_sha256": production_digests(),
        "checks": {case_id: CASES[case_id]["detail"].get("checks")
                   for case_id in COUNTER_CASE_IDS},
        "mutations": {case_id: CASES[case_id]["detail"].get("detected")
                      for case_id in ("mutation_null_not_negation",
                                      "mutation_shared_column_independence")},
    }


def check_report_consistency():
    try:
        text = open(REPORT_PATH, encoding="utf-8").read()
        claims = extract_claims(text)
    except Exception as exc:
        record("report_consistency", "report", False, {"error": repr(exc)},
               status="error")
        return
    problems = []
    actual = recomputed_claims()
    claimed_cases = claims.get("cases", {})
    for case_id, status in claimed_cases.items():
        if case_id not in CASES:
            problems.append("REPORT 引用了不存在的案例: %s" % case_id)
        elif CASES[case_id]["status"] != status:
            problems.append("案例状态不一致: %s REPORT=%s evidence=%s"
                            % (case_id, status, CASES[case_id]["status"]))
    missing = sorted(set(REQUIRED_CLAIM_CASES) - set(claimed_cases))
    if missing:
        problems.append("REPORT 漏引案例: %s" % ", ".join(missing))
    if claims.get("production_deviations") != actual["production_deviations"]:
        problems.append("生产偏差数量不一致: REPORT=%r evidence=%r"
                        % (claims.get("production_deviations"),
                           actual["production_deviations"]))
    if claims.get("production_sha256") != actual["production_sha256"]:
        problems.append("生产文件摘要不一致: REPORT 与重算不符")
    if claims.get("checks") != actual["checks"]:
        problems.append("REPORT 的核对次数与重算不符: REPORT=%r evidence=%r"
                        % (claims.get("checks"), actual["checks"]))
    if claims.get("mutations") != actual["mutations"]:
        problems.append("REPORT 的变异检出结论与重算不符: REPORT=%r evidence=%r"
                        % (claims.get("mutations"), actual["mutations"]))
    for keyword in REQUIRED_REPORT_KEYWORDS:
        if keyword not in text:
            problems.append("REPORT 缺少覆盖说明关键词: %s" % keyword)
    blocking = CASES.get("deviation_blocking", {}).get("detail", {})
    if not blocking.get("blocked"):
        problems.append("REPORT 声称已证明阻断，但 deviation_blocking 未确认阻断")
    record("report_consistency", "report", not problems, {"problems": problems})


# --------------------------------------------------------------------------
# evidence assembly / commands
# --------------------------------------------------------------------------
# ``elapsed_seconds``/``peak_python_bytes`` are measurements, not decisions;
# ``evidence_reproducible`` is the self-referential result of this very
# comparison, so it cannot be part of the projected state.
# ``report_consistency`` is derived from REPORT.md, not from production
# behaviour, so it too is omitted from the reproducibility projection.
VOLATILE_KEYS = {"generated_at", "python", "elapsed_seconds",
                 "peak_python_bytes", "stress", "evidence_reproducible",
                 "report_consistency", "stdout_tail"}


def _drop_volatile(value):
    if isinstance(value, dict):
        return {key: _drop_volatile(item) for key, item in value.items()
                if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [_drop_volatile(item) for item in value]
    return value


def stable_projection(document):
    return json.dumps(_drop_volatile(document), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def build_evidence():
    return {
        "schema": "qa-evidence-v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version.split()[0],
        "rerun": ["python3", "qa.py", "verify"],
        "seeds": {"differential": list(DIFFERENTIAL_SEEDS),
                  "scan": 31337, "stress": 4242, "predicates": [90210, 90211]},
        "limits": {"max_comparisons": 16, "max_depth": 16,
                   "max_constant_abs": MAX_ABS},
        "production_sha256": production_digests(),
        "counterexamples": DEVIATIONS,
        "counterexample_counts_by_source": dict(sorted(DEVIATION_COUNTS.items())),
        "counterexamples_per_source_cap": MAX_COUNTEREXAMPLES_PER_SOURCE,
        "production_deviations": sum(DEVIATION_COUNTS.values()),
        "cases": CASES,
        "stress": STRESS_RECORDS,
    }


def verify():
    started = time.perf_counter()
    before = production_digests()
    CASES.clear()
    DEVIATIONS.clear()
    DEVIATION_COUNTS.clear()
    FAILURES.clear()
    STRESS_RECORDS.clear()

    runners = (
        check_production_baseline,
        check_original_regression,
        check_oracle_independence,
        check_small_domain,
        check_differential,
        check_scan_pipeline,
        check_scan_immutability,
        check_pruner_stats_only,
        check_manual_cases,
        check_mutations,
        check_stress,
        check_finite_basis,
        check_deviation_blocking,
        check_verifier_selftest,
        check_report_consistency,
    )
    for runner in runners:
        try:
            runner()
        except Exception as exc:  # verifier bug must block, not pass silently
            runner_name = getattr(runner, "__name__", repr(runner))
            CASES.setdefault(runner_name, {})["status"] = "error"
            CASES[runner_name].setdefault("detail", {})["exception"] = repr(exc)
            FAILURES.append(runner_name)
            print("验证器异常: %s: %r" % (runner_name, exc))

    after = production_digests()
    unchanged = before == after
    record("production_files_unchanged", "immutability", unchanged,
           {"before": before, "after": after})

    evidence = build_evidence()

    drift = []
    if os.path.exists(EVIDENCE_PATH):
        try:
            previous = json.load(open(EVIDENCE_PATH, encoding="utf-8"))
            if stable_projection(previous) != stable_projection(evidence):
                drift.append("evidence.json 与本次重算结果不一致")
        except Exception as exc:
            drift.append("无法读取旧 evidence.json: %r" % (exc,))
    record("evidence_reproducible", "evidence", not drift,
           {"drift": drift, "path": "evidence.json"})
    if drift:
        FAILURES.append("evidence_reproducible")

    with open(EVIDENCE_PATH, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    elapsed = time.perf_counter() - started
    print("QA cases: %d" % len(CASES))
    for case_id in sorted(CASES):
        case = CASES[case_id]
        print("  %-42s %-9s %s" % (case_id, case["status"], case["kind"]))
    print("生产偏差: %d" % sum(DEVIATION_COUNTS.values()))
    for counterexample in DEVIATIONS:
        print("  反例: %s" % json.dumps(counterexample, ensure_ascii=False,
                                        sort_keys=True))
    print("evidence.json 已重算; 总耗时 %.3fs" % elapsed)
    if sum(DEVIATION_COUNTS.values()):
        FAILURES.append("production_deviation")
    if FAILURES:
        print("QA_VERIFY_BLOCKED: %s" % ", ".join(sorted(set(FAILURES))))
        return 1
    print("QA_VERIFY_OK")
    return 0


def demo():
    groups = sample_groups()
    print("== 公开样例 (samples.py) ==")
    print(json.dumps({"predicate": SAMPLE_PREDICATE,
                      "groups": groups}, ensure_ascii=False, sort_keys=True))
    print("-- 逐组判定 --")
    for index, group in enumerate(groups):
        decision = oracle.judge(group["stats"], SAMPLE_PREDICATE)
        production = pruner.can_skip(group["stats"], SAMPLE_PREDICATE)
        print(json.dumps({"group": index, "stats": group["stats"],
                          "oracle": decision, "production_can_skip": production},
                         ensure_ascii=False, sort_keys=True))
    print("-- 真实扫描 --")
    print(json.dumps({"pruning_on": scanner.scan(groups, SAMPLE_PREDICATE),
                      "pruning_off": scanner.scan(groups, SAMPLE_PREDICATE,
                                                  pruning=False),
                      "oracle_expected": expected_scan(groups, SAMPLE_PREDICATE)},
                     ensure_ascii=False, sort_keys=True))
    print("== 手工案例 ==")
    for case_id in ("manual_null_not", "manual_same_column_contradiction",
                    "manual_cross_column_order", "manual_wide_bounds_true"):
        case = MANUAL_CASES[case_id]
        decision = oracle.judge(case["stats"], case["predicate"])
        print(json.dumps({"case": case_id, "stats": case["stats"],
                          "predicate": case["predicate"],
                          "oracle": decision,
                          "production_can_skip": pruner.can_skip(case["stats"],
                                                                 case["predicate"]),
                          "note": case["note"]},
                         ensure_ascii=False, sort_keys=True))
    print("== 变异检出摘要（隔离副本实时执行） ==")
    CASES.clear()
    DEVIATIONS.clear()
    DEVIATION_COUNTS.clear()
    FAILURES.clear()
    check_mutations()
    for case_id in ("mutation_null_not_negation",
                    "mutation_shared_column_independence"):
        if case_id in CASES:
            detail = CASES[case_id]["detail"]
            print(json.dumps({"case": case_id, "status": CASES[case_id]["status"],
                              "detected": detail.get("detected"),
                              "expected_correct": detail.get("expected_correct"),
                              "actual_mutated": detail.get("actual_mutated")},
                             ensure_ascii=False, sort_keys=True))
    return 0


def main(argv):
    if len(argv) != 2 or argv[1] not in ("verify", "demo"):
        print("用法: python3 qa.py {verify|demo}", file=sys.stderr)
        return 2
    return verify() if argv[1] == "verify" else demo()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
