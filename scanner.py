"""Scan stored groups using separately callable metadata and row paths."""
from copy import deepcopy
from pruner import can_skip
from semantics import evaluate

def scan(groups, predicate, *, pruning=True):
    rows, skipped = [], []
    for index, group in enumerate(groups):
        if pruning and can_skip(group["stats"], predicate):
            skipped.append(index)
            continue
        for row in group["data"]:
            if evaluate(predicate, row) is True:
                rows.append(deepcopy(row))
    return {"rows": rows, "skipped": skipped}
