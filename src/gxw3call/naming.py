"""Type library names -> Python names.

bstrProjectPath -> project_path, lpdwError -> error, wcNameStr -> name_str,
GetPOUNames -> get_pou_names. Used by tools/gen_api.py for signatures and by
the generated api module for the keys of returned values.
"""

import keyword
import re

# lpbCallID, bstrProjectPath, dwPlcSeries, wTimeOut, pbIsReadOnly ...
HUNGARIAN = re.compile(
    r'^(lppp?st|lpbstr|lpst|lpsz|lpdw|lpull|lpul|lpuc|lpby|lpw|lpl|lpb|lpv|lp'
    r'|bstr|ldw|dw|ull|ul|uc|by|st|sz|wc|pb|pl|w|l|b|p)(?=[A-Z])'
)
_WORD_BREAK = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')


def snake(name):
    """GetPOUNames -> get_pou_names, IsPlcOnline -> is_plc_online."""
    return _WORD_BREAK.sub('_', name).lower()


def python_names(names):
    """{type library name: python name}, unique within the given names.

    The prefix is dropped unless that makes two names equal (lpdwSize and
    dwSize would both become size); those keep their prefix.
    """
    short = {n: snake(HUNGARIAN.sub('', n)) for n in names}
    counts = {}
    for value in short.values():
        counts[value] = counts.get(value, 0) + 1
    out = {}
    for name in names:
        py = short[name] if counts[short[name]] == 1 else snake(name)
        out[name] = py + '_' if keyword.iskeyword(py) else py
    return out


def convert_keys(value):
    """Rename the keys of a returned value, structs and arrays included."""
    if isinstance(value, dict):
        names = python_names(list(value))
        return {names[k]: convert_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [convert_keys(v) for v in value]
    return value
