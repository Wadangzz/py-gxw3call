"""Call every read-only looking method once with default arguments.

usage: probe_readonly.py <project.gx3> <out dir> [interface ...]

Opens a copy of the project and calls each Get*/Is*/Check* method of the
given interfaces (default: all). Records hresult, return code and a summary
of the outputs in <out dir>/probe.json. Nothing is saved to the project.

Defaults: lpbCallID = the opened project, *Path = the project copy, other
strings empty, numbers 0, timeouts 10. A call that hangs or crashes the
worker is recorded and the worker (and its hidden GX) is restarted.
"""

import json
import os
import re
import shutil
import subprocess
import sys

from gxw3call import typelib
from gxw3call.client import DEFAULT_TLB, Gx

READ_ONLY = re.compile(r'(Get|Is|Check)[A-Z]')
SKIP = re.compile(r'Dialog')
OUT_ARRAY = 4096  # elements reserved behind every out scalar pointer
OUT_STRUCTS = 16
CALL_TIMEOUT = 90


def default_args(method, tlb, project_path):
    args, alloc = {}, {}
    for p in method.params:
        if p.name in ('lpbCallID', 'lplReturnCode'):
            continue  # call id: filled per session; return code: one long
        if p.type == 'BSTR' and p.name.endswith('ProjectPath'):
            args[p.name] = project_path
        elif p.name == 'wTimeOut':
            args[p.name] = 10
        base = p.type[:-1] if p.type.endswith('*') else None
        if p.direction != 'in' and base in typelib.SCALAR_SIZE:
            alloc[p.name] = OUT_ARRAY
        elif p.direction != 'in' and base in tlb.records:
            alloc[p.name] = OUT_STRUCTS
    return args, alloc


def summarize(value):
    """Keep the record small: long arrays and raw dumps are cut."""
    if isinstance(value, list):
        used = len(value)
        while used and value[used - 1] == 0:
            used -= 1
        if len(value) == OUT_ARRAY and used <= 1:  # a scalar read as an array
            return value[0]
        return [summarize(v) for v in value[: min(used, 8)]]
    if isinstance(value, dict):
        return {
            k: (f'{len(v) // 2} bytes' if k == 'raw' else summarize(v))
            for k, v in value.items()
        }
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + '...'
    return value


def kill_hidden_gx():
    """Only GX instances started by the Open Interface, never a user's GUI."""
    subprocess.run(
        [
            'powershell',
            '-NoProfile',
            '-Command',
            'Get-CimInstance Win32_Process -Filter "Name=\'GXW3.exe\'" | '
            "Where-Object { $_.CommandLine -match 'OPENIFGXW3' } | "
            'ForEach-Object { Stop-Process -Id $_.ProcessId -Force }',
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


class Session:
    def __init__(self, project_path, log):
        self.project_path, self.log = project_path, log
        self.gx = self.call_id = None
        self.start()

    def start(self):
        self.gx = Gx(log=self.log)
        self.gx.call_timeout = 240
        self.call_id = self.gx.open_project(self.project_path).call_id
        self.gx.call_timeout = CALL_TIMEOUT

    def restart(self):
        self.gx._proc.kill()
        kill_hidden_gx()
        self.start()

    def close(self):
        self.gx.call_timeout = 60
        self.gx.call(
            'IGXW3CallProject',
            'CloseProject',
            lpbCallID=self.call_id,
            lOption=0,
        )
        self.gx.close()


def main(source, out_dir, *only):
    os.makedirs(out_dir, exist_ok=True)
    project_path = shutil.copy(source, os.path.join(out_dir, 'probe.gx3'))
    tlb = typelib.load(DEFAULT_TLB)
    session = Session(project_path, os.path.join(out_dir, 'worker.log'))
    results = {}
    try:
        for iface in tlb.interfaces.values():
            if (only and iface.name not in only) or not iface.clsid:
                continue
            for method in iface.methods.values():
                if not READ_ONLY.match(method.name) or SKIP.search(
                    method.name
                ):
                    continue
                args, alloc = default_args(method, tlb, project_path)
                if any(
                    p.name == 'lpbCallID' and p.direction == 'in'
                    for p in method.params
                ):
                    args['lpbCallID'] = session.call_id
                key = f'{iface.name}.{method.name}'
                try:
                    hr, out = session.gx.call(
                        iface.name, method.name, alloc=alloc, **args
                    )
                    rc = out.pop('lplReturnCode', None)
                    out.pop('lpbCallID', None)
                    results[key] = {
                        'hr': hr,
                        'rc': None if rc is None else rc & 0xFFFFFFFF,
                        'args': {
                            k: v for k, v in args.items() if k != 'lpbCallID'
                        },
                        'out': summarize(out),
                    }
                except RuntimeError as exc:  # marshalling gap or COM error
                    results[key] = {'error': str(exc)}
                except OSError as exc:  # timeout or dead worker
                    results[key] = {
                        'error': f'worker lost: {type(exc).__name__}: {exc}'
                    }
                    session.restart()
                r = results[key]
                print(
                    key,
                    r.get('error') or f'hr={r["hr"]:#x} rc={r["rc"]}',
                    flush=True,
                )
                with open(
                    os.path.join(out_dir, 'probe.json'), 'w', encoding='utf-8'
                ) as f:
                    json.dump(results, f, ensure_ascii=False, indent=1)
    finally:
        try:
            session.close()
        except (OSError, RuntimeError):
            kill_hidden_gx()


if __name__ == '__main__':
    main(*sys.argv[1:])
