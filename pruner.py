"""Production metadata pruning via Boolean alternatives and difference bounds."""
from itertools import product

INVERSE = {"=": "!=", "!=": "=", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}
NODE = {"x": 1, "y": 2}

def _edge(a, b, strict=0):
    # a <[=] b is encoded as node(a)-node(b) <= offset(b)-offset(a)-strict.
    i, c = (NODE[a], 0) if isinstance(a, str) else (0, a)
    j, d = (NODE[b], 0) if isinstance(b, str) else (0, b)
    return (i, j, d - c - strict)

def _alternatives(p, want, nulls):
    op, *args = p
    if op == "not":
        return _alternatives(args[0], not want, nulls)
    if op in ("and", "or"):
        left = _alternatives(args[0], want, nulls)
        right = _alternatives(args[1], want, nulls)
        if (op == "and") == want:
            return [a + b for a in left for b in right]
        return left + right
    a, b = args
    if any(isinstance(v, str) and v in nulls for v in args):
        return []
    if not want:
        op = INVERSE[op]
    if op == "!=":
        return [[_edge(a, b, 1)], [_edge(b, a, 1)]]
    if op == "=":
        return [[_edge(a, b), _edge(b, a)]]
    if op in (">", ">="):
        a, b = b, a
    return [[_edge(a, b, int(op in ("<", ">")))]]

def _feasible(edges):
    d = [[0 if i == j else None for j in range(3)] for i in range(3)]
    for i, j, c in edges:
        if d[i][j] is None or c < d[i][j]:
            d[i][j] = c
    for k in range(3):
        for i in range(3):
            for j in range(3):
                if d[i][k] is not None and d[k][j] is not None:
                    v = d[i][k] + d[k][j]
                    if d[i][j] is None or v < d[i][j]:
                        d[i][j] = v
    return all(d[i][i] >= 0 for i in range(3))

def can_skip(stats, predicate):
    """Exact impossibility under the public metadata envelope, without row reads."""
    n = stats["rows"]
    if n == 0:
        return True
    choices = []
    for col in ("x", "y"):
        s = stats[col]
        choices.append(([True] if s["nulls"] else []) +
                       ([False] if s["nulls"] < n else []))
    for flags in product(*choices):
        nulls = {col for col, flag in zip(("x", "y"), flags) if flag}
        bounds = []
        for col in ("x", "y"):
            if col not in nulls:
                s = stats[col]
                bounds.extend([_edge(s["lo"], col), _edge(col, s["hi"])])
        for edges in _alternatives(predicate, True, nulls):
            if _feasible(bounds + edges):
                return False
    return True
