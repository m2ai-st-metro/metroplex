# Smoke Test Calculator

Build a single Python module `calculator.py` exposing two functions:

- `add(a, b)` -- returns the sum of a and b
- `subtract(a, b)` -- returns a minus b

Both functions accept int or float and return the same type.

## Tests

Include `test_calculator.py` with pytest test cases covering:

- Positive numbers: `add(2, 3) == 5`, `subtract(5, 3) == 2`
- Negative numbers: `add(-1, -1) == -2`, `subtract(-1, -2) == 1`
- Zero: `add(0, 0) == 0`, `subtract(5, 5) == 0`
- Floats: `add(1.5, 2.5) == 4.0`, `subtract(5.0, 2.5) == 2.5`

## Constraints

- Pure Python standard library only. No external dependencies.
- Must pass `pytest test_calculator.py -v` with 100% of tests green.
- Single source file + single test file. No packaging.
