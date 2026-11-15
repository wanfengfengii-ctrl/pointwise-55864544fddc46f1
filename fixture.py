"""Writer only: builds valid groups, without predicate or expected-output logic."""
from copy import deepcopy

def group(rows):
    rows = deepcopy(rows)
    for row in rows:
        if set(row) != {"x", "y"}:
            raise ValueError("two columns required")
        if any(v is not None and type(v) is not int for v in row.values()):
            raise ValueError("nullable integers required")
    stats = {"rows": len(rows)}
    for col in ("x", "y"):
        values = [r[col] for r in rows if r[col] is not None]
        stats[col] = {"nulls": len(rows) - len(values),
                      "lo": min(values) if values else None,
                      "hi": max(values) if values else None}
    return {"stats": stats, "data": rows}
