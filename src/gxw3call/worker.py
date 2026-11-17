"""32-bit worker: hosts GXW3Call.dll and serves vtable calls over a socket.

GXW3Call.dll is a 32-bit in-process COM server, so it cannot be loaded into a
64-bit backend. This module runs under 32-bit Python, connects back to the
client's listening port and answers one JSON request per line.

The interfaces are dual, but CallID is a C array (byte[32]) that late binding
cannot pass, so every call goes through the vtable. Arguments are marshalled
from the type library signature (see typelib.py).

Request : {"iface": "IGXW3CallProject", "method": "OpenProject",
           "args": {...}, "alloc": {...}, "count": {...}}
Response: {"ok": true, "hr": 0, "out": {...}} | {"ok": false, "error": "..."}
Bytes travel as hex strings.
"""

import argparse
import ctypes
import json
import re
import socket
import traceback
from ctypes import wintypes

from gxw3call import typelib

CLSCTX_INPROC_SERVER = 1
# Slack behind a caller-allocated out struct. The callee may write far more
# than the type library size: GetDefaultConnection declares 504 bytes and
# writes past 4600, which killed the worker with a 4 KB pad.
OUT_STRUCT_PAD = 1 << 16
SCALAR_CTYPE = {
    'byte': ctypes.c_ubyte,
    'char': ctypes.c_byte,
    'short': ctypes.c_short,
    'ushort': ctypes.c_ushort,
    'long': ctypes.c_long,
    'ulong': ctypes.c_ulong,
    'int': ctypes.c_int,
    'uint': ctypes.c_uint,
    'int64': ctypes.c_int64,
    'uint64': ctypes.c_uint64,
    'ULONG_PTR': ctypes.c_size_t,
}

ole32 = ctypes.OleDLL('ole32')
oleaut32 = ctypes.WinDLL('oleaut32')
oleaut32.SysAllocStringLen.restype = ctypes.c_void_p
oleaut32.SysAllocStringLen.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]


class GUID(ctypes.Structure):
    _fields_ = [
        ('d1', wintypes.DWORD),
        ('d2', wintypes.WORD),
        ('d3', wintypes.WORD),
        ('d4', ctypes.c_ubyte * 8),
    ]


def guid(text):
    g = GUID()
    ole32.CLSIDFromString(ctypes.c_wchar_p('{' + text + '}'), ctypes.byref(g))
    return g


def bstr(text):
    return ctypes.c_void_p(oleaut32.SysAllocStringLen(text, len(text)))


def read_bstr(ptr):
    return ctypes.wstring_at(ptr) if ptr else ''


class Host:
    def __init__(self, tlb):
        self.tlb = tlb
        self.objects = {}
        ole32.CoInitialize(None)

    def object(self, iface):
        """COM pointer for an interface. Created and Initialize(null)d once."""
        if iface.name not in self.objects:
            ptr = ctypes.c_void_p()
            ole32.CoCreateInstance(
                ctypes.byref(guid(iface.clsid)),
                None,
                CLSCTX_INPROC_SERVER,
                ctypes.byref(guid(iface.iid)),
                ctypes.byref(ptr),
            )
            self.objects[iface.name] = ptr
            if 'Initialize' in iface.methods:
                self.call(iface.name, 'Initialize', {}, {}, {})
        return self.objects[iface.name]

    def call(self, iface_name, method_name, args, alloc, count):
        iface = self.tlb.interfaces[iface_name]
        method = iface.methods[method_name]
        this = self.object(iface)
        values, argtypes, readers, keep = [], [], [], []
        for p in method.params:
            value, ctype, reader = self._marshal(
                p, args.get(p.name), alloc, keep
            )
            values.append(value)
            argtypes.append(ctype)
            if reader:
                readers.append((p, reader))
        vtable = ctypes.cast(this, ctypes.POINTER(ctypes.c_void_p))[0]
        entry = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[
            method.vtbl
        ]
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
        hr = proto(entry)(this, *values)
        out = {}
        for p, reader in readers:
            out[p.name] = reader()
        self._follow_pointers(method, out, count)
        return hr, out

    def _marshal(self, p, given, alloc, keep):
        """-> (value to pass, ctypes type, reader() for out values or None)."""
        t, is_out = p.type, p.direction != 'in'
        tlb = self.tlb
        array = re.fullmatch(r'byte\[(\d+)\]', t)
        if array:
            buf = ctypes.create_string_buffer(
                bytes.fromhex(given) if given else b'', int(array.group(1))
            )
            keep.append(buf)
            return (
                buf,
                ctypes.c_void_p,
                (lambda: buf.raw.hex()) if is_out else None,
            )
        if t in SCALAR_CTYPE:
            return given or 0, SCALAR_CTYPE[t], None
        if t == 'BSTR':
            return bstr(given or ''), ctypes.c_void_p, None
        if t == 'BSTR*':
            holder = bstr(given) if given is not None else ctypes.c_void_p()
            keep.append(holder)
            reader = (lambda: read_bstr(holder.value)) if is_out else None
            return ctypes.byref(holder), ctypes.c_void_p, reader
        if t == 'IUnknown*':
            return None, ctypes.c_void_p, None
        base = t[:-1] if t.endswith('*') else None
        if base in ('void', 'char') or (base == 'byte' and not is_out):
            buf = ctypes.create_string_buffer(bytes.fromhex(given or ''))
            keep.append(buf)
            return buf, ctypes.c_void_p, None
        if base in SCALAR_CTYPE:
            # scalar out, or an array of known length (alloc = element count)
            n = alloc.get(p.name, len(given) if isinstance(given, list) else 1)
            arr = (SCALAR_CTYPE[base] * n)()
            for i, v in enumerate(
                given if isinstance(given, list) else [given or 0]
            ):
                arr[i] = v
            keep.append(arr)
            # read back even when declared [in]: GetPlcType marks outputs so
            return (
                arr,
                ctypes.c_void_p,
                lambda: arr[0] if n == 1 else list(arr),
            )
        if base in tlb.records:
            # caller-allocated struct (or array of them): raw bytes in and out
            size = tlb.size_of(base)
            raw = self._struct_bytes(base, given)
            total = max(len(raw), size * alloc.get(p.name, 1)) + OUT_STRUCT_PAD
            buf = ctypes.create_string_buffer(raw, total)
            keep.append(buf)
            reader = None
            if is_out:
                n = alloc.get(p.name, 1)
                reader = (  # noqa: E731
                    lambda: {
                        'raw': buf.raw[: size * n].hex(),
                        'value': [
                            tlb.decode(base, buf.raw, i * size)
                            for i in range(n)
                        ],
                    }
                )
            return buf, ctypes.c_void_p, reader
        if t in tlb.records:  # struct by value
            raw = self._struct_bytes(t, given)
            by_value = type(
                'ByValue',
                (ctypes.Structure,),
                {
                    '_pack_': 1,
                    '_fields_': [('b', ctypes.c_ubyte * tlb.size_of(t))],
                },
            )
            return (
                by_value.from_buffer_copy(raw.ljust(tlb.size_of(t), b'\0')),
                by_value,
                None,
            )
        if t.endswith('**'):
            # callee-allocated: pointer comes back, read by _follow_pointers
            holder = ctypes.c_void_p()
            keep.append(holder)
            return (
                ctypes.byref(holder),
                ctypes.c_void_p,
                lambda: holder.value or 0,
            )
        raise NotImplementedError(f'{p.direction} {t} {p.name}')

    def _struct_bytes(self, record, given):
        """A struct argument: hex dump, dict, or a list of either (array)."""
        if not given:
            return b''
        items = given if isinstance(given, list) else [given]
        return b''.join(self.tlb.encode(record, item) for item in items)

    def _follow_pointers(self, method, out, count):
        """Read callee-allocated arrays (T**). Length = count override, else
        the out ulong* parameter right before it (lpdwSize + lppstNames)."""
        prev = None
        for p in method.params:
            if p.direction != 'in' and re.fullmatch(r'[^*]+\*\*', p.type):
                elem, ptr = p.type[:-2], out[p.name]
                n = count.get(p.name)
                if (
                    n is None
                    and prev is not None
                    and prev.type in ('ulong*', 'long*')
                ):
                    n = out.get(prev.name)
                    if isinstance(
                        n, list
                    ):  # count was read as an oversized array
                        n = n[0]
                result = {'pointer': ptr}
                if ptr and isinstance(n, int) and 0 < n <= 100000:
                    if elem in self.tlb.records or elem in typelib.SCALAR_SIZE:
                        size = self.tlb.size_of(elem)
                        raw = ctypes.string_at(ptr, size * n)
                        result['raw'] = raw.hex()
                        result['value'] = [
                            self.tlb.decode(elem, raw, i * size)
                            for i in range(n)
                        ]
                out[p.name] = result
            prev = p


def serve(port, tlb_path):
    host = Host(typelib.load(tlb_path))
    sock = socket.create_connection(('127.0.0.1', port))
    stream = sock.makefile('rwb')
    for line in stream:
        req = json.loads(line)
        if req.get('op') == 'quit':
            break
        try:
            hr, out = host.call(
                req['iface'],
                req['method'],
                req.get('args', {}),
                req.get('alloc', {}),
                req.get('count', {}),
            )
            resp = {'ok': True, 'hr': hr & 0xFFFFFFFF, 'out': out}
        except Exception as exc:  # report every failure to the client
            resp = {
                'ok': False,
                'error': f'{type(exc).__name__}: {exc}',
                'trace': traceback.format_exc(),
            }
        stream.write(json.dumps(resp).encode() + b'\n')
        stream.flush()
    sock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--connect', type=int, required=True)
    parser.add_argument('--tlb', required=True)
    ns = parser.parse_args()
    serve(ns.connect, ns.tlb)
