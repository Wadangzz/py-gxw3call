"""Dump the CPU and module catalogs GX Works3 exposes read-only, as JSON.

usage: dump_catalog.py <project.gx3> <out dir> [io number ...]

Starts GX once: opens a copy of the project hidden, reads everything through
that one session and closes without saving. Writes:

  series.json     CPU series (id, name)
  plc_types.json  per series: CPU types (id, name) with the language and
                  operation mode lists GX reports for each type
  units_all.json  GetUnitDataListAll: every rack unit of every series.
                  The list for one CPU is this filtered by wSeries
                  (R16 = series 121 + 122, checked against GetUnitDataList)
  units_open_project.json  GetUnitDataList for the opened project's CPU
  network.json    per io number: what the network configuration readers
                  return for the opened project (rc and data)
  catalog.json    all of the above

The network readers need the start I/O number of a network module. Pass the
candidates to try (hex, e.g. 0 20 40); default 0 20 40.
"""

import json
import os
import shutil
import sys

from gxw3call.client import Gx

PRODUCT = 'IGXW3CallProduct'
COMM = 'IGXW3CallCommunication'
# (method, extra args) of readers that take wIONumber
NETWORK_READERS = (
    ('GetStationType', {'wNetworkType': 0}),
    ('GetCCLinkMode', {}),
    ('GetCCLinkConfig', {}),
    ('GetCCLinkRefreshParam', {}),
    ('GetCCLinkIEFieldConfig', {}),
    ('GetCCIENexusConfig', {}),
)


def product(gx, call_id, method, array, **args):
    _, out = gx.call(PRODUCT, method, lpbCallID=call_id, wTimeOut=30, **args)
    rc = out['lplReturnCode'] & 0xFFFFFFFF
    return rc, out[array].get('value', []) if rc == 0 else []


def save(out_dir, name, data):
    with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def main(source, out_dir, *io_numbers):
    os.makedirs(out_dir, exist_ok=True)
    copy = shutil.copy(source, os.path.join(out_dir, 'catalog_source.gx3'))
    ios = [int(x, 16) for x in io_numbers] or [0x0, 0x20, 0x40]
    catalog = {}
    with Gx(log=os.path.join(out_dir, 'worker.log')) as gx:
        gx.call_timeout = 300
        project = gx.open_project(copy)  # OpenProject starts GX hidden
        call_id = project.call_id
        try:
            catalog['open_project'] = {
                'cpu': project.plc_type_string(),
                'hardware': project.hardware(),
            }
            _, series = product(
                gx, call_id, 'GetPlcSeriesList', 'lpstPlcSeriesArray'
            )
            catalog['series'] = series
            types = {}
            for s in series:
                _, rows = product(
                    gx,
                    call_id,
                    'GetPlcTypeList',
                    'lpstPlcTypeArray',
                    dwPlcSeries=s['dwID'],
                )
                for t in rows:
                    for key, method, array in (
                        (
                            'languages',
                            'GetLanguageTypeList',
                            'lpstLanguageTypeArray',
                        ),
                        (
                            'operation_modes',
                            'GetOperationModeList',
                            'lpstOperationModeList',
                        ),
                    ):
                        rc, t[key] = product(
                            gx, call_id, method, array, dwPlcType=t['dwID']
                        )
                        if rc:
                            t[key + '_rc'] = hex(rc)
                types[s['wcNameStr']] = {'series': s['dwID'], 'types': rows}
            catalog['plc_types'] = types
            _, catalog['units_all'] = product(
                gx, call_id, 'GetUnitDataListAll', 'lpstUnitDataList'
            )
            _, catalog['units_open_project'] = product(
                gx, call_id, 'GetUnitDataList', 'lpstUnitDataList'
            )
            network = {}
            for io in ios:
                for method, extra in NETWORK_READERS:
                    _, out = gx.call(
                        COMM, method, lpbCallID=call_id, wIONumber=io, **extra
                    )
                    out.pop('lpbCallID', None)
                    rc = out.pop('lplReturnCode') & 0xFFFFFFFF
                    network.setdefault(f'{io:#x}', {})[method] = {
                        'rc': hex(rc),
                        'out': out,
                    }
                    print(f'io={io:#x}', method, 'rc', hex(rc), flush=True)
            catalog['network'] = network
        finally:
            project.close()
    for name in (
        'series',
        'plc_types',
        'units_all',
        'units_open_project',
        'network',
    ):
        save(out_dir, name + '.json', catalog[name])
    save(out_dir, 'catalog.json', catalog)
    print(
        'series',
        len(series),
        'types',
        sum(len(v['types']) for v in types.values()),
        'units_all',
        len(catalog['units_all']),
        'units_open_project',
        len(catalog['units_open_project']),
    )


if __name__ == '__main__':
    main(*sys.argv[1:])
