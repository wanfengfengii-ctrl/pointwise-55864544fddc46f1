"""Retain the original business assertions during quality work."""
import json
import sys
from copy import deepcopy
from pruner import can_skip
from scanner import scan
from samples import groups, PREDICATE

def normal():
    data = groups()
    before = deepcopy(data)
    result = scan(data, PREDICATE)
    assert result == {"rows": [{"x": 3, "y": 5}, {"x": 3, "y": 5}],
                      "skipped": [0, 2]}
    assert scan(data, PREDICATE, pruning=False) == {
        "rows": result["rows"], "skipped": []}
    assert [can_skip(g["stats"], PREDICATE) for g in data] == [True, False, True]
    assert data == before
    print("SCAN_NORMAL_OK")
    return result

if __name__ == "__main__":
    result = normal()
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
