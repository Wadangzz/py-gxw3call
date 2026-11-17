"""Measure what a call changes: call on a copy, save, diff with a control.

usage: probe_changes.py <project.gx3> <out dir> [method ...]

For every case (method + one argument combination):
  mutating  : fresh copy -> open -> call -> save -> close -> compare the .gx3
              with a control copy that was only opened and saved
  read-like : called in one shared session; outputs and written files kept

Recorded per case in <out dir>/changes.json: hresult, return code, outputs,
files the call wrote, POU list and compiled flag afterwards, and the .gx3
entries that differ from the control. Entries that already differ between
two control runs (timestamps) are ignored.

The source project must be converted and should contain a program POU, an
FB, a structure and a global label list; names are read from it.
"""

import hashlib
import json
import os
import shutil
import sys
import zipfile

from probe_readonly import kill_hidden_gx, summarize

from gxw3call.client import PROJECT, Gx, GxError

CALL_TIMEOUT = 180
OBJECT_TYPES = range(0, 9)


def entries(path):
    """{zip entry: md5} without GX's internal history folder."""
    with zipfile.ZipFile(path) as z:
        return {
            i.filename: hashlib.md5(z.read(i)).hexdigest()
            for i in z.infolist()
            if not i.filename.endswith('/')
            and not i.filename.startswith('.h/')
        }


def diff(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def files_in(folder):
    out = {}
    for root, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(root, name)
            out[os.path.relpath(path, folder)] = os.path.getsize(path)
    return out


def cases(names, out_dir, other):
    """-> [(label, method, args, mutating)]. Paths are placeholders filled
    per case with that case's folder."""
    pou, fb, sdt, glist = (
        names['pou'],
        names['fb'],
        names['sdt'],
        names['glist'],
    )
    c = []

    def add(method, mutating, label='', **args):
        c.append((f'{method}({label})', method, args, mutating))

    # --- read-like: results come back as outputs or as files
    for mode in range(0, 4):
        add(
            'ExportProjectXmlExt',
            False,
            f'mode={mode}',
            bstrExportFilePath='{dir}\\project.xml',
            bFormat=1,
            lExportMode=mode,
        )
    for obj, name in (
        ('pou', pou),
        ('fb', fb),
        ('sdt', sdt),
        ('glist', glist),
    ):
        for t in OBJECT_TYPES:
            add(
                'ExportObjectXml',
                False,
                f'{obj},type={t}',
                bstrExportFilePath='{dir}\\object.xml',
                bFormat=1,
                bstrObjectName=name,
                lObjectType=t,
            )
    for program in names['programs']:
        add(
            'ExportProgramViewHtml',
            False,
            program,
            bstrProgramName=program,
            bstrExportFilePath='{dir}\\program.html',
        )
        add('GetProgramNo', False, program, bstrProgramName=program)
    add(
        'ExportUsedDeviceAndLabel', False, bstrExportFilePath='{dir}\\used.csv'
    )
    add(
        'ExportLibrary',
        False,
        bstrFilePath='{dir}\\lib.usl',
        bAllowOverwrite=1,
    )
    for t in range(0, 12):
        add(
            'ExportParameterXml',
            False,
            f'unit={t}',
            bstrExportFilePath='{dir}\\param.xml',
            lUnitType=t,
        )
    for t in range(0, 6):
        for name in (glist, pou, fb):
            add(
                'GetLabels',
                False,
                f'list={t},{name}',
                lLabelListType=t,
                bstrLabelListName=name,
                lClassFilter=0,
                lRequestedProperties=0,
            )
    add(
        'SimpleVerifyProject',
        False,
        'same file',
        bstrProjectPath=other,
        bstrReportFilePath='{dir}\\verify.txt',
        bstrSourceHeader='src',
        bstrDestinationHeader='dst',
    )

    # --- mutating: one fresh copy each
    for status in range(0, 4):
        add('SetProjectStatus', True, f'status={status}', dwStatus=status)
    for obj, name in (
        ('pou', pou),
        ('fb', fb),
        ('sdt', sdt),
        ('glist', glist),
    ):
        for t in OBJECT_TYPES:
            add(
                'RenameObject',
                True,
                f'{obj},type={t}',
                bstrObjectName=name,
                lObjectType=t,
                bstrNewObjectName='ZZ_RENAMED',
            )
            add(
                'DeleteProjectViewObject',
                True,
                f'{obj},type={t}',
                bstrObjectName=name,
                lObjectType=t,
            )
    for save_type in range(0, 3):
        add(
            'SaveAsProject',
            True,
            f'saveType={save_type}',
            bstrNewProjectPath='{dir}\\saved_as.gx3',
            bstrNewTitle='saved',
            lSaveType=save_type,
            lInheritanceRights=0,
        )
    for flag in range(0, 3):
        add(
            'SaveFileStartupData',
            True,
            f'flag={flag}',
            bstrProjectPath='{dir}\\startup.gx3',
            wSaveFlag=flag,
        )
    for label_assign in range(0, 4):
        for check in (0, 1):
            add(
                'RebuildProject',
                True,
                f'{label_assign},{check}',
                lLabelAssignmentType=label_assign,
                bCheckProgram=check,
            )
    add('BuildProject', True)
    add(
        'ChangePlcType',
        True,
        'to R32',
        dwPlcSeries=0xA,
        dwPlcType=0x1004,
        bstrUserName='',
        bstrPassword='',
        bstrNewProjectPath='{dir}\\changed.gx3',
        bOverwrite=1,
    )
    return c


def fill(args, folder):
    return {
        k: v.replace('{dir}', folder) if isinstance(v, str) else v
        for k, v in args.items()
    }


def read_names(project):
    pous = project.pou_names()
    out = project.call('GetSDTNames')
    sdts = [r['wcNameStr'] for r in out['lppstNames'].get('value', [])]
    out = project.call('GetGlobalLabelListNames')
    glists = [r['wcNameStr'] for r in out['lppstNames'].get('value', [])]
    out = project.call('GetFileListForDownload')
    programs = [
        r['wcFileName']
        for r in out['lpstFileList'].get('value', [])
        if r['wFileType'] == 5
    ]
    fbs = [p for p in pous if p.startswith('FB_')]
    plain = [p for p in pous if p not in fbs and p != 'ProgPou']
    return {
        'pou': plain[-1],
        'fb': fbs[0],
        'sdt': sdts[0],
        'glist': glists[0],
        'programs': programs[:2],
        'all_pous': pous,
    }


def call_case(gx, project, method, args):
    try:
        hr, out = gx.call(PROJECT, method, lpbCallID=project.call_id, **args)
    except RuntimeError as exc:
        return {'error': str(exc)}
    out.pop('lpbCallID', None)
    rc = out.pop('lplReturnCode', None)
    return {
        'hr': hr,
        'rc': None if rc is None else f'{rc & 0xFFFFFFFF:#x}',
        'out': summarize(out),
    }


def state(project):
    try:
        return {'pous': project.pou_names(), 'compiled': project.is_compiled()}
    except (GxError, RuntimeError) as exc:
        return {'state_error': str(exc)}


def open_saved(gx, source, folder, name='p.gx3'):
    os.makedirs(folder, exist_ok=True)
    path = shutil.copy(source, os.path.join(folder, name))
    return path, gx.open_project(path)


def main(source, out_dir, *only):
    os.makedirs(out_dir, exist_ok=True)
    log = os.path.join(out_dir, 'worker.log')
    results = {}

    def new_gx():
        gx = Gx(log=log)
        gx.call_timeout = CALL_TIMEOUT
        return gx

    gx = new_gx()
    # two controls: what open + save alone changes, and what is noise
    controls = []
    for n in (1, 2):
        path, project = open_saved(
            gx, source, os.path.join(out_dir, f'control{n}')
        )
        if n == 1:
            names = read_names(project)
        project.save()
        project.close()
        controls.append(entries(path))
    noise = set(diff(controls[0], controls[1]))
    print('names', names, '| noise entries', sorted(noise), flush=True)
    other = shutil.copy(source, os.path.join(out_dir, 'other.gx3'))
    todo = [
        c for c in cases(names, out_dir, other) if not only or c[1] in only
    ]

    def record(label, result):
        results[label] = result
        brief = result.get('error') or (
            f'rc={result["rc"]} files={list(result.get("files", {}))} '
            f'changed={len(result.get("changed", []))}'
        )
        print(label, '->', brief, flush=True)
        with open(
            os.path.join(out_dir, 'changes.json'), 'w', encoding='utf-8'
        ) as f:
            json.dump(results, f, ensure_ascii=False, indent=1)

    # read-like cases share one session
    path, project = open_saved(gx, source, os.path.join(out_dir, 'shared'))
    for n, (label, method, args, mutating) in enumerate(todo):
        if mutating:
            continue
        folder = os.path.join(out_dir, 'read', f'{n:03}')
        os.makedirs(folder, exist_ok=True)
        try:
            result = call_case(gx, project, method, fill(args, folder))
            result['files'] = files_in(folder)
        except OSError as exc:  # worker lost: restart and reopen
            result = {'error': f'worker lost: {exc}'}
            gx._proc.kill()
            kill_hidden_gx()
            gx = new_gx()
            path, project = open_saved(
                gx, source, os.path.join(out_dir, 'shared')
            )
        record(label, result)
    try:
        project.close()
    except (GxError, RuntimeError, OSError):
        kill_hidden_gx()

    for n, (label, method, args, mutating) in enumerate(todo):
        if not mutating:
            continue
        folder = os.path.join(out_dir, 'write', f'{n:03}')
        try:
            path, project = open_saved(gx, source, folder)
            try:
                result = call_case(gx, project, method, fill(args, folder))
                result.update(state(project))
                project.save()
            finally:
                project.close()
            changed = [
                e for e in diff(controls[0], entries(path)) if e not in noise
            ]
            result['changed'] = changed
            files = files_in(folder)
            files.pop('p.gx3', None)
            result['files'] = files
        except (OSError, GxError) as exc:
            result = {'error': f'{type(exc).__name__}: {exc}'}
            gx._proc.kill()
            kill_hidden_gx()
            gx = new_gx()
        record(label, result)
    gx.close()
    kill_hidden_gx()


if __name__ == '__main__':
    main(*sys.argv[1:])
