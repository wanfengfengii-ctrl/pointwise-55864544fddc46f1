"""Small public input, also printed as JSON by this module."""
import json
from fixture import group

PREDICATE = ["and", [">=", "x", 3], ["<", "x", "y"]]

def groups():
    return [
        group([{"x": 1, "y": 2}, {"x": 2, "y": 9}]),
        group([{"x": 3, "y": 5}, {"x": 3, "y": 5}, {"x": None, "y": 8}]),
        group([{"x": 8, "y": 2}, {"x": 9, "y": 3}])
    ]

if __name__ == "__main__":
    print(json.dumps({"groups": groups(), "predicate": PREDICATE}, ensure_ascii=False))
