"""Client side: start the 32-bit worker and call GX Works3 through it.

  with Gx() as gx:
    project = gx.create_project(r'D:\\work\\a.gx3', 'R16')
    project.import_plc_files(r'D:\\work\\prm', ['SYSTEM', 'CPU', 'UNIT'])
    project.import_xml(r'D:\\work\\a.xml')
    project.rebuild()
    project.save()
    project.close()


Works from 64-bit Python. Every method of the COM interfaces is reachable with
gx.call(interface, method, **args); the Project class only wraps the calls
whose arguments have been measured.
"""

import glob
import json
import os
import socket
import struct
import subprocess

PROJECT = 'IGXW3CallProject'
PRODUCT = 'IGXW3CallProduct'
NO_CALL_ID = '00' * 32
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TLB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data', 'GXW3Call_tlb.txt'
)
# file type codes of _ST_PLC_FILE_DATA.wFileType (measured)
PLC_FILE_TYPE = {'SYSTEM': 7, 'CPU': 8, 'UNIT': 9, 'MEMCARD': 11}
PLC_FILE_DRIVE = {'MEMCARD': 2}  # others live on drive 4
# lUserInterfaceMode (measured from the GXW3.exe command line): 0 and 1 start
# GX with a visible window that stays after CloseProject; 2 starts it hidden
# and the process exits on CloseProject. One GX process serves one project.
UI_HIDDEN = 2


class GxError(RuntimeError):
    def __init__(self, method, rc, out):
        super().__init__(f'{method}: rc=0x{rc & 0xFFFFFFFF:08x}')
        self.method, self.rc, self.out = method, rc & 0xFFFFFFFF, out


def find_python32():
    """GXW3CALL_PYTHON32, else a uv-managed 32-bit CPython."""
    env = os.environ.get('GXW3CALL_PYTHON32')
    if env:
        return env
    pattern = os.path.join(
        os.environ['APPDATA'],
        'uv',
        'python',
        'cpython-*-windows-x86-none',
        'python.exe',
    )
    found = sorted(glob.glob(pattern))
    if not found:
        raise FileNotFoundError(
            '32-bit Python not found. Set GXW3CALL_PYTHON32 or run '
            '"uv python install cpython-3.12-windows-x86".'
        )
    return found[-1]


class Gx:
    def __init__(self, tlb=DEFAULT_TLB, python32=None, log=None):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        # The worker must not inherit a pipe: GXW3.exe keeps inherited handles
        # open and a reader on that pipe never sees EOF. Log to a file instead.
        self._log = open(log or os.devnull, 'ab')
        self._proc = subprocess.Popen(
            [
                python32 or find_python32(),
                '-m',
                'gxw3call.worker',
                '--connect',
                str(listener.getsockname()[1]),
                '--tlb',
                tlb,
            ],
            env={**os.environ, 'PYTHONPATH': SRC_DIR},
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=self._log,
        )
        listener.settimeout(30)
        self._sock, _ = listener.accept()
        listener.close()
        self._stream = self._sock.makefile('rwb')
        self.call_timeout = None  # seconds per call; None = wait forever
        self._plc_types = None

    def call(self, iface, method, alloc=None, count=None, **args):
        """-> (hresult, {out parameter name: value}). Bytes are hex strings."""
        # GX rejects forward slashes in paths (ImportPlcFormatData: 0x0601100b)
        args = {
            k: os.path.abspath(v) if _is_path(k, v) else v
            for k, v in args.items()
        }
        req = {
            'iface': iface,
            'method': method,
            'args': args,
            'alloc': alloc or {},
            'count': count or {},
        }
        self._sock.settimeout(self.call_timeout)
        self._stream.write(json.dumps(req).encode() + b'\n')
        self._stream.flush()
        line = self._stream.readline()
        if not line:
            raise ConnectionError(f'worker died during {iface}.{method}')
        resp = json.loads(line)
        if not resp['ok']:
            raise RuntimeError(f'{iface}.{method}: {resp["error"]}')
        return resp['hr'], resp['out']

    def close(self):
        try:
            self._stream.write(b'{"op": "quit"}\n')
            self._stream.flush()
        except OSError:
            pass
        self._sock.close()
        try:
            self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # stuck inside a COM call
            self._proc.kill()
        self._log.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def open_project(self, path, timeout=180):
        _, out = self.call(
            PROJECT,
            'OpenProject',
            bstrProjectPath=path,
            wTimeOut=timeout,
            lUserInterfaceMode=UI_HIDDEN,
            pbIsReadOnly=0,
        )
        _check('OpenProject', out)
        return Project(self, out['lpbCallID'])

    def plc_types(self):
        """{CPU name: (series id, type id)} as GX lists them, e.g. 'R16' ->
        (0xA, 0x1003). Needs no open project (the call id may be all zero).
        Read once per Gx; it reflects the installed GX version."""
        if self._plc_types is None:
            types = {}
            _, out = self.call(
                PRODUCT, 'GetPlcSeriesList', lpbCallID=NO_CALL_ID, wTimeOut=30
            )
            for series in _check('GetPlcSeriesList', out)[
                'lpstPlcSeriesArray'
            ]['value']:
                _, out = self.call(
                    PRODUCT,
                    'GetPlcTypeList',
                    lpbCallID=NO_CALL_ID,
                    dwPlcSeries=series['dwID'],
                    wTimeOut=30,
                )
                for row in _check('GetPlcTypeList', out)['lpstPlcTypeArray'][
                    'value'
                ]:
                    types[row['wcNameStr']] = (series['dwID'], row['dwID'])
            self._plc_types = types
        return self._plc_types

    def create_project(self, path, cpu, title='', timeout=180):
        """cpu: the name GX uses ('R16', 'R32EN', 'FX5U', 'L04H');
        the model name
        with a CPU suffix ('R16CPU') is accepted too. Ladder is the language
        (dwLanguageType 1)."""
        types = self.plc_types()
        name = cpu if cpu in types else cpu.removesuffix('CPU')
        if name not in types:
            raise ValueError(f'unknown CPU {cpu!r}. GX lists: {sorted(types)}')
        series, plc_type = types[name]
        _, out = self.call(
            PROJECT,
            'CreateNewProject',
            bstrProjectPath=path,
            bstrTitle=title or os.path.splitext(os.path.basename(path))[0],
            lSaveType=0,
            dwPlcSeries=series,
            dwPlcType=plc_type,
            dwLanguageType=1,
            wTimeOut=timeout,
            lUserInterfaceMode=UI_HIDDEN,
        )
        _check('CreateNewProject', out)
        return Project(self, out['lpbCallID'])


def _is_path(name, value):
    return (
        isinstance(value, str) and value and name.endswith(('Path', 'Folder'))
    )


def _check(method, out, allow=()):
    rc = out.get('lplReturnCode', 0) & 0xFFFFFFFF
    if rc != 0 and rc not in allow:
        raise GxError(method, rc, out)
    return out


class Project:
    def __init__(self, gx, call_id):
        self.gx, self.call_id = gx, call_id

    def call(self, method, allow=(), alloc=None, count=None, **args):
        _, out = self.gx.call(
            PROJECT,
            method,
            alloc=alloc,
            count=count,
            lpbCallID=self.call_id,
            **args,
        )
        return _check(method, out, allow)

    def close(self):
        self.call('CloseProject', lOption=0)

    def save(self):
        self.call('SaveProject')

    def plc_type_string(self):
        return self.call('GetPlcTypeString')['lpszPlcTypeString']

    def plc_type(self):
        """-> (series, type) for create_project."""
        out = self.call('GetPlcType')
        return out['lpdwPlcSeries'], out['lpdwPlcType']

    def is_compiled(self):
        return bool(self.call('IsProjectCompiled')['lpbIsCompiled'])

    def pou_names(self):
        out = self.call('GetPOUNames', lPOUType=0)
        return [row['wcNameStr'] for row in out['lppstNames'].get('value', [])]

    def hardware(self):
        """Mounted units: [(base, slot, unit code, io number)]."""
        info = self.call('GetSystemHardwareConfiguration')['lpSystem'][
            'value'
        ][0]
        units = []
        for b, base in enumerate(info['stBase']):
            for s, slot in enumerate(base['stSlot']):
                if slot['wUnitCode']:
                    units.append((b, s, slot['wUnitCode'], slot['wIONum']))
        return units

    def import_plc_files(self, folder, names=('SYSTEM', 'CPU', 'UNIT')):
        """Import <folder>\\$MELPRJ$\\<name>.PRM, one call per file, in order.

        The bytes are the PRM as stored inside a .gx3 (no flag, no footer).
        rc 0 does not prove the result: read back hardware() / the saved file.
        A UNIT.PRM saved by GX after a rebuild has zeroed module positions and
        breaks here; one written by the exporter is fine.
        """
        for name in names:
            path = os.path.join(folder, '$MELPRJ$', name + '.PRM')
            entry = plc_file_entry(
                PLC_FILE_TYPE[name],
                name,
                PLC_FILE_DRIVE.get(name, 4),
                os.path.getsize(path),
            )
            self.call(
                'ImportPlcFormatData',
                dwPlcFileDataSize=1,
                lpstPlcFileDataList=entry.hex(),
                bstrTargetFolder=folder,
            )

    def import_xml(self, path):
        """PLCopen XML -> POUs, labels, comments. Import the PRMs first: in an
        empty project a ladder XML only partly arrives (rc 0x1903)."""
        return self.call(
            'ImportProjectXml',
            bstrImportFilePath=path,
            lImportFlags=0,
            lImportMode=1,
            bstrReservedForFutureUse='',
        )['lplImportErrors']

    def export_xml(self, path):
        """Ladder bodies are only present for converted POUs. rc 0x1901 comes
        back with a valid file, so it is not treated as an error."""
        return self.call(
            'ExportProjectXml',
            allow=(0x1901,),
            bstrExportFilePath=path,
            bFormat=1,
        )['lplExportErrors']

    def rebuild(self):
        """Rebuild All. -> (errors, warnings). lLabelAssignmentType 2 is the
        value that works after an XML import (0 -> 0x1001, 1 -> 0x1aa6)."""
        out = self.call(
            'RebuildProject', lLabelAssignmentType=2, bCheckProgram=0
        )
        return out['lpdwError'], out['lpdwWarning']


def plc_file_entry(file_type, name, drive, size):
    """_ST_PLC_FILE_DATA, pack 1, 561 bytes. The type library declares natural
    alignment (568) but the native side reads pack 1."""
    buf = bytearray(561)
    struct.pack_into('<H', buf, 0, file_type)
    raw = name.encode('utf-16-le')
    buf[2 : 2 + len(raw)] = raw
    struct.pack_into('<i', buf, 516, drive)
    struct.pack_into('<Q', buf, 553, size)
    return bytes(buf)
