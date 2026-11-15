"""Row predicate evaluation; None denotes SQL UNKNOWN."""
import operator

OPS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt,
       "<=": operator.le, ">": operator.gt, ">=": operator.ge}

def evaluate(predicate, row):
    op, *args = predicate
    if op == "not":
        value = evaluate(args[0], row)
        return None if value is None else not value
    if op in ("and", "or"):
        a, b = (evaluate(p, row) for p in args)
        if op == "and":
            return False if a is False or b is False else (
                True if a is True and b is True else None)
        return True if a is True or b is True else (
            False if a is False and b is False else None)
    a, b = (row[v] if isinstance(v, str) else v for v in args)
    return None if a is None or b is None else OPS[op](a, b)
