"""Generate docs/API.md from the type library dump and the measured notes.

usage: gen_doc.py [out.md]

Inputs (all in this repository):
  src/gxw3call/data/GXW3Call_tlb.txt  signatures and structs
  docs/head.md                        hand-written introduction
  notes/measured.json                 per-method results of real calls
  notes/probe_readonly.json           default-argument probe results, used
                                      only where no measured note exists
Probe cells show the return code and the names of the outputs, not their
values: the values come from whatever project the probe opened.
"""

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TLB = os.path.join(REPO, 'src', 'gxw3call', 'data', 'GXW3Call_tlb.txt')
HEAD = os.path.join(REPO, 'docs', 'head.md')
NOTES = os.path.join(REPO, 'notes', 'measured.json')
PROBE = os.path.join(REPO, 'notes', 'probe_readonly.json')
OUT = os.path.join(REPO, 'docs', 'API.md')

BASE = {
    'QueryInterface',
    'AddRef',
    'Release',
    'GetTypeInfoCount',
    'GetTypeInfo',
    'GetIDsOfNames',
    'Invoke',
}


def parse(path):
    items, cur = [], None
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
                items.append(cur)
            elif line.startswith('  ') and cur is not None:
                cur['rows'].append(line.strip())
    return items


def probe_cells(probe, iface, name):
    """Default-argument probe: weaker evidence than a measured note."""
    r = probe.get(f'{iface}.{name}')
    if r is None:
        return '', ''
    if 'error' in r:
        return 'probed', f'Default arguments: {r["error"]}.'
    if r['rc'] in (0, None) and r['hr'] == 0:
        names = ', '.join(f'`{k}`' for k in r.get('out', {}))
        return 'probed', 'Default arguments return 0.' + (
            f' Outputs: {names}.' if names else ''
        )
    if r['rc'] is None:
        return 'probed', f'Default arguments: hresult 0x{r["hr"]:x}.'
    return 'probed', f'Default arguments return 0x{r["rc"]:x}.'


def cell(parts):
    return '<br>'.join(f'`{p}`' for p in parts) if parts else ''


def render(items, notes, probe):
    out = []
    ifaces = [
        i
        for i in items
        if i['kind'] == 'DISPATCH'
        and any(r.startswith('FUNC') for r in i['rows'])
        and i['name'] != 'IComponentRegistrar'
    ]
    coclass = {i['name']: i['guid'] for i in items if i['kind'] == 'COCLASS'}
    out.append('## Interfaces\n')
    out.append('| Interface | coclass CLSID | Methods |')
    out.append('| --- | --- | --- |')
    for i in ifaces:
        n = sum(
            1 for r in i['rows'] if r.split('(')[0].split()[-1] not in BASE
        )
        clsid = coclass.get('GXW3Call' + i['name'][9:], '')
        out.append(f'| `{i["name"]}` | `{clsid}` | {n} |')
    out.append('')
    for i in ifaces:
        out.append(f'## {i["name"]}\n')
        out.append(f'IID `{i["guid"]}`\n')
        out.append('| vtbl | Method | In | Out | Status | Notes |')
        out.append('| --- | --- | --- | --- | --- | --- |')
        for idx, row in enumerate(i['rows']):
            m = re.match(r'FUNC \S+ (\w+)\((.*)\)$', row)
            if not m or m.group(1) in BASE:
                continue
            name, ins, outs = m.group(1), [], []
            for p in filter(None, (x.strip() for x in m.group(2).split(', '))):
                d, t, n = re.match(r'\[([^\]]*)\] (.+) (\S+)$', p).groups()
                (outs if 'out' in d else ins).append(
                    f'{n}: {t}' + (' (inout)' if d.startswith('inout') else '')
                )
            entry = notes.get(name)
            if entry:
                status, note = entry['status'], entry['note']
            else:
                status, note = probe_cells(probe, i['name'], name)
            note = note.replace('|', '/')
            out.append(
                f'| {idx} | `{name}` | {cell(ins)} | {cell(outs)} '
                f'| {status} | {note} |'
            )
        out.append('')
    out.append('## Structs\n')
    out.append(
        'Fields follow each other with pack 1. The real size can be larger '
        'than declared.\n'
    )
    for i in items:
        if i['kind'] != 'RECORD':
            continue
        out.append(f'### {i["name"]}\n')
        out.append('| Field | Type |')
        out.append('| --- | --- |')
        for r in i['rows']:
            n, t = r.split(' : ')
            out.append(f'| `{n}` | `{t}` |')
        out.append('')
    return '\n'.join(out)


def main(argv):
    path = argv[1] if len(argv) > 1 else OUT
    with open(NOTES, encoding='utf-8') as f:
        notes = json.load(f)
    probe = {}
    if os.path.exists(PROBE):
        with open(PROBE, encoding='utf-8') as f:
            probe = json.load(f)
    with open(HEAD, encoding='utf-8') as f:
        head = f.read()
    body = render(parse(TLB), notes, probe)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(head.rstrip('\n') + '\n\n' + body.rstrip('\n') + '\n')
    print(f'{path}: {len(notes)} measured, {len(probe)} probed')


if __name__ == '__main__':
    main(sys.argv)
