"""Method and struct signatures of GXW3Call, read from a type library dump.

The dump is produced locally by tools/tlb_dump.ps1 from the installed DLL and
is not part of this repository. Format (one item per block):

  TKIND_DISPATCH IGXW3CallProject <iid> funcs=96 vars=0
    FUNC void OpenProject([in] BSTR bstrProjectPath, ..., [out] long* rc)
  TKIND_RECORD _ST_OBJNAME <guid> funcs=0 vars=1
    wcName : ushort[257]
"""

import re
from dataclasses import dataclass, field

from gxw3call.naming import python_names

POINTER_SIZE = 4  # the DLL is 32-bit
SCALAR_SIZE = {
    'byte': 1,
    'char': 1,
    'short': 2,
    'ushort': 2,
    'long': 4,
    'ulong': 4,
    'int': 4,
    'uint': 4,
    'int64': 8,
    'uint64': 8,
    'ULONG_PTR': 4,
}
_IUNKNOWN_IDISPATCH = 7  # vtable slots before the first own method


@dataclass
class Param:
    name: str
    type: str
    direction: str  # 'in' | 'out' | 'inout'


@dataclass
class Method:
    name: str
    vtbl: int
    params: list[Param]


@dataclass
class Interface:
    name: str
    iid: str
    clsid: str
    methods: dict[str, Method] = field(default_factory=dict)


@dataclass
class TypeLib:
    interfaces: dict[str, Interface]
    records: dict[str, list[tuple[str, str]]]

    def size_of(self, type_name):
        """Byte size with pack 1 (measured for _ST_PLC_FILE_DATA = 561)."""
        m = re.fullmatch(r'(.+)\[(\d+)\]', type_name)
        if m:
            return self.size_of(m.group(1)) * int(m.group(2))
        if type_name.endswith('*') or type_name in ('BSTR', 'LPWSTR'):
            return POINTER_SIZE
        if type_name in SCALAR_SIZE:
            return SCALAR_SIZE[type_name]
        return sum(self.size_of(t) for _, t in self.records[type_name])

    def decode(self, type_name, data, offset=0):
        """Raw bytes -> python value, pack 1. Pointers become integers."""
        m = re.fullmatch(r'(.+)\[(\d+)\]', type_name)
        if m:
            elem, n = m.group(1), int(m.group(2))
            step = self.size_of(elem)
            return [
                self.decode(elem, data, offset + i * step) for i in range(n)
            ]
        if type_name in self.records:
            out = {}
            for name, t in self.records[type_name]:
                value = self.decode(t, data, offset)
                out[name] = _as_text(name, t, value)
                offset += self.size_of(t)
            return out
        size = self.size_of(type_name)
        signed = type_name in _SIGNED
        return int.from_bytes(
            data[offset : offset + size], 'little', signed=signed
        )

    def encode(self, type_name, value):
        """Python value -> raw bytes, the inverse of decode.

        A struct is a dict keyed by type library names or by their Python
        names (naming.python_names); missing fields are zero. Text fields
        take a str. Hex strings pass through, so a raw dump can be reused.
        """
        if isinstance(value, str) and type_name in self.records:
            return bytes.fromhex(value)
        m = re.fullmatch(r'(.+)\[(\d+)\]', type_name)
        if m:
            elem, n = m.group(1), int(m.group(2))
            if isinstance(value, str):
                value = _text_units(elem, value)
            items = list(value) + [0] * (n - len(value))
            if len(items) > n:
                raise ValueError(f'{type_name}: {len(value)} items')
            return b''.join(self.encode(elem, v) for v in items)
        if type_name in self.records:
            fields = self.records[type_name]
            python = python_names([name for name, _ in fields])
            out = b''
            for name, t in fields:
                given = value.get(name, value.get(python[name]))
                if given is None:
                    out += bytes(self.size_of(t))
                else:
                    out += self.encode(t, given)
            return out
        size = self.size_of(type_name)
        signed = type_name in _SIGNED
        return int(value or 0).to_bytes(size, 'little', signed=signed)


_SIGNED = ('char', 'short', 'long', 'int', 'int64')
_TEXT_WIDE = re.compile(r'wc|wsz|sz')  # szHost : ushort[100] is text too
_TEXT_NARROW = re.compile(r'c[A-Z]|sz')


def _text_units(elem, text):
    if elem == 'ushort':
        raw = text.encode('utf-16-le')
        return [
            int.from_bytes(raw[i : i + 2], 'little')
            for i in range(0, len(raw), 2)
        ]
    return list(text.encode('ascii'))


def _as_text(name, type_name, value):
    """wcName : ushort[257] and cPassword : char[33] are text, not numbers."""
    if re.fullmatch(r'ushort\[\d+\]', type_name) and _TEXT_WIDE.match(name):
        chars = value[: value.index(0)] if 0 in value else value
        return ''.join(map(chr, chars))
    if re.fullmatch(r'char\[\d+\]', type_name) and _TEXT_NARROW.match(name):
        chars = value[: value.index(0)] if 0 in value else value
        return bytes(c & 0xFF for c in chars).decode('ascii', 'replace')
    return value


def load(path):
    blocks, cur = [], None
    with open(path, encoding='utf-8-sig') as f:
        for line in f:
            line = line.rstrip('\n')
            if line.startswith('TKIND_'):
                kind, name, guid = line.split()[:3]
                cur = {
                    'kind': kind[6:],
                    'name': name,
                    'guid': guid,
                    'rows': [],
                }
                blocks.append(cur)
            elif line.startswith('  ') and cur is not None:
                cur['rows'].append(line.strip())

    coclass = {b['name']: b['guid'] for b in blocks if b['kind'] == 'COCLASS'}
    interfaces, records = {}, {}
    for b in blocks:
        if b['kind'] == 'RECORD':
            records[b['name']] = [tuple(r.split(' : ')) for r in b['rows']]
            continue
        funcs = [r for r in b['rows'] if r.startswith('FUNC')]
        if b['kind'] != 'DISPATCH' or not funcs:
            continue
        # IGXW3CallProject -> coclass GXW3CallProject
        clsid = coclass.get('GXW3Call' + b['name'][len('IGXW3Call') :], '')
        iface = Interface(b['name'], b['guid'], clsid)
        for vtbl, row in enumerate(funcs):
            m = re.match(r'FUNC \S+ (\w+)\((.*)\)$', row)
            if vtbl < _IUNKNOWN_IDISPATCH:
                continue
            params = []
            for p in filter(None, (x.strip() for x in m.group(2).split(', '))):
                direction, type_name, name = re.match(
                    r'\[([^\]]*)\] (.+) (\S+)$', p
                ).groups()
                params.append(Param(name, type_name, direction))
            iface.methods[m.group(1)] = Method(m.group(1), vtbl, params)
        interfaces[b['name']] = iface
    return TypeLib(interfaces, records)
