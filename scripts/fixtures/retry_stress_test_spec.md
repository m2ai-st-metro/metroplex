# Retry Stress Test: Temperature Converter with Validation

Build a single Python module `temp_converter.py` with the following functions:

- `celsius_to_fahrenheit(c)` -- returns the Fahrenheit equivalent
- `fahrenheit_to_celsius(f)` -- returns the Celsius equivalent
- `kelvin_to_celsius(k)` -- returns the Celsius equivalent
- `validate_temperature(value, scale)` -- returns True if the temperature is physically valid (at or above absolute zero for the given scale), raises `ValueError` with message "Temperature below absolute zero" otherwise

Scale constants for absolute zero:
- Celsius: -273.15
- Fahrenheit: -459.67
- Kelvin: 0

All conversion functions must call `validate_temperature` before converting. If the input is below absolute zero for its scale, the conversion function must let the `ValueError` propagate (do not catch it).

## Tests

Include `test_temp_converter.py` with pytest test cases covering:

### Basic conversions
- `celsius_to_fahrenheit(0)` == `32.0`
- `celsius_to_fahrenheit(100)` == `212.0`
- `celsius_to_fahrenheit(-40)` == `-40.0`
- `fahrenheit_to_celsius(32)` == `0.0`
- `fahrenheit_to_celsius(212)` == `100.0`
- `kelvin_to_celsius(273.15)` == `0.0`
- `kelvin_to_celsius(0)` == `-273.15`

### Validation (absolute zero enforcement)
- `celsius_to_fahrenheit(-274)` raises `ValueError`
- `fahrenheit_to_celsius(-460)` raises `ValueError`
- `kelvin_to_celsius(-1)` raises `ValueError`
- `validate_temperature(-273.15, "C")` returns `True`  (boundary: exactly absolute zero is valid)
- `validate_temperature(-273.16, "C")` raises `ValueError`

### Float precision
- `celsius_to_fahrenheit(37)` must equal `98.6` exactly (body temperature)
- `fahrenheit_to_celsius(98.6)` must equal `37.0` exactly

### Edge case: unknown scale
- `validate_temperature(100, "R")` raises `ValueError` with message "Unknown scale: R"

## Constraints

- Pure Python standard library only. No external dependencies.
- Must pass `pytest test_temp_converter.py -v` with 100% of tests green.
- Single source file + single test file. No packaging.
- All functions must accept int or float. Return type is always float.
- Use `pytest.approx` for float comparisons in tests where needed.

## Why This Spec Triggers Retries

This spec is DESIGNED to cause a first-attempt failure that is recoverable on retry.
The trap: the float precision tests demand `celsius_to_fahrenheit(37) == 98.6` exactly.

The naive formula `c * 9/5 + 32` applied to 37 yields `98.60000000000001` in IEEE 754
floating point. A Builder that writes the straightforward formula and uses exact equality
(`==`) in tests will see those two precision tests fail.

The fix is simple -- either use `pytest.approx` in the assertions (the spec hints at this
in the Constraints section, but Builders tend to only use approx where they think it's
needed), or use `round()` in the implementation. The Judge should diagnose the floating
point mismatch and instruct the Builder to fix the assertions or the rounding on retry.

If the Builder reads carefully and uses `pytest.approx` everywhere from the start, the
spec still works as a valid build -- it just passes on attempt 1 instead of triggering a
retry, which is an acceptable outcome for a stress test (it means the Builder is good).
