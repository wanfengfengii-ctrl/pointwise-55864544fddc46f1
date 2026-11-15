"""Independent exact decision procedure for two-column metadata pruning.

This module is a quality oracle.  It does not import, call, or copy the
production pruner (`pruner.py`), the production row evaluator
(`semantics.py`), any Boolean branch expansion, difference-constraint
closure, or an external solver.  It implements the public contract directly
with a finite sign-cell abstraction over the metadata envelope.

The two columns live in independent integer envelopes supplied by the
statistics.  Every atom in the predicate compares either one column with an
integer constant, one column with the other, or two constants.  Therefore the
truth value of every atom is fixed once we know, for each non-NULL column,

  * which cell of the arrangement of the predicate's integer constants it
    occupies (equal to a constant, below the next constant, and so on), and
  * for the two columns jointly, the sign of ``x - y`` (<, =, >).

There are finitely many such cells: at most ``2*k + 1`` per column for ``k``
distinct constants (the contract caps the predicate at 16 comparison leaves,
so ``k <= 32``).  Representative integers are chosen inside each cell; a
strict or non-strict comparison between cells is feasible exactly when the
corresponding interval inequality is feasible.  Evaluating the predicate at
these representatives covers every possible truth vector of the predicate
over the whole (possibly astronomically wide) integer envelope, so no
integer-by-integer enumeration is needed and the procedure is exact.

NULL is handled by enumerating the metadata-allowed NULL/not-NULL flag
combinations (a column may be NULL iff ``nulls > 0`` and non-NULL iff
``nulls < rows``); when an operand is NULL every comparison is UNKNOWN, which
`evaluate` reproduces directly.
"""

UNKNOWN = None


def _cmp(op, left, right):
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise ValueError("unknown comparison operator: %r" % (op,))


def evaluate(predicate, row):
    """Three-valued row semantics written from the contract, not imported."""
    head = predicate[0]
    if head == "not":
        value = evaluate(predicate[1], row)
        return UNKNOWN if value is UNKNOWN else not value
    if head == "and":
        left = evaluate(predicate[1], row)
        right = evaluate(predicate[2], row)
        if left is False or right is False:
            return False
        if left is True and right is True:
            return True
        return UNKNOWN
    if head == "or":
        left = evaluate(predicate[1], row)
        right = evaluate(predicate[2], row)
        if left is True or right is True:
            return True
        if left is False and right is False:
            return False
        return UNKNOWN
    left, right = predicate[1], predicate[2]
    if isinstance(left, str):
        left = row[left]
    if isinstance(right, str):
        right = row[right]
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return _cmp(head, left, right)


def _collect_constants(node, out):
    head = node[0]
    if head == "not":
        _collect_constants(node[1], out)
    elif head in ("and", "or"):
        _collect_constants(node[1], out)
        _collect_constants(node[2], out)
    else:
        for operand in (node[1], node[2]):
            if isinstance(operand, int) and not isinstance(operand, bool):
                out.add(operand)


def _cells(lo, hi, cuts):
    """Integer intervals partitioning [lo, hi] by the predicate's constants."""
    points = sorted(c for c in cuts if lo <= c <= hi)
    cells = []
    start = lo
    for point in points:
        if start < point:
            cells.append((start, point - 1))
        cells.append((point, point))
        start = point + 1
    if start <= hi:
        cells.append((start, hi))
    return cells


def _joint_pair(x_cell, y_cell, relation):
    """A concrete (x, y) in the two cells realizing the requested sign."""
    xa, xb = x_cell
    ya, yb = y_cell
    if relation == "<":
        if xa >= yb:
            return None
        return (xa, max(ya, xa + 1))
    if relation == "=":
        point = max(xa, ya)
        return (point, point) if point <= min(xb, yb) else None
    if relation == ">":
        if ya >= xb:
            return None
        return (max(xa, ya + 1), ya)
    raise ValueError("unknown relation: %r" % (relation,))


def judge(stats, predicate):
    """Return {"skip": bool, "witness": row-or-None} exactly per contract."""
    rows = stats["rows"]
    if rows == 0:
        return {"skip": True, "witness": None}

    cuts = set()
    _collect_constants(predicate, cuts)

    modes = {}
    for col in ("x", "y"):
        column = stats[col]
        options = []
        if column["nulls"] > 0:
            options.append(None)
        if column["nulls"] < rows:
            options.extend(_cells(column["lo"], column["hi"], cuts))
        modes[col] = options

    for x_mode in modes["x"]:
        for y_mode in modes["y"]:
            if x_mode is None or y_mode is None:
                row = {"x": None if x_mode is None else x_mode[0],
                       "y": None if y_mode is None else y_mode[0]}
                if evaluate(predicate, row) is True:
                    return {"skip": False, "witness": row}
                continue
            for relation in ("<", "=", ">"):
                pair = _joint_pair(x_mode, y_mode, relation)
                if pair is None:
                    continue
                row = {"x": pair[0], "y": pair[1]}
                if evaluate(predicate, row) is True:
                    return {"skip": False, "witness": row}
    return {"skip": True, "witness": None}
