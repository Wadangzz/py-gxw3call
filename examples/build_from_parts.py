"""Create a project from parts through the API and compare it to the source.

usage: build_from_parts.py <source.gx3> <prm folder with $MELPRJ$> <work dir>

source -> export XML; new project <- PRMs <- XML -> rebuild -> export XML;
then the two XMLs must hold the same ladder instructions.
"""

import os
import re
import shutil
import sys

from gxw3call.client import Gx


def instructions(path):
    with open(path, 'rb') as f:
        text = f.read().decode('utf-16')  # GX writes UTF-16 with BOM
    return re.findall(r'<instruction[^>]*content="([^"]*)"', text)


def main(source, prm_folder, work):
    os.makedirs(work, exist_ok=True)
    src = shutil.copy(source, os.path.join(work, 'src.gx3'))
    src_xml, out_xml = (
        os.path.join(work, 'src.xml'),
        os.path.join(work, 'out.xml'),
    )
    new = os.path.join(work, 'created.gx3')
    if os.path.exists(new):
        os.remove(new)
    with Gx(log=os.path.join(work, 'worker.log')) as gx:
        project = gx.open_project(src)
        cpu = project.plc_type_string()
        print('source', cpu)
        print('  pous', project.pou_names())
        print(
            '  hardware', [(b, s, hex(c)) for b, s, c, _ in project.hardware()]
        )
        project.export_xml(src_xml)
        project.close()

        project = gx.create_project(new, cpu)
        try:  # a project left open keeps a GX process and the file lock alive
            project.import_plc_files(prm_folder)
            print(
                'created hardware',
                [(b, s, hex(c)) for b, s, c, _ in project.hardware()],
            )
            print('  xml import errors', project.import_xml(src_xml))
            print('  rebuild (errors, warnings)', project.rebuild())
            print(
                '  compiled',
                project.is_compiled(),
                'pous',
                len(project.pou_names()),
            )
            project.export_xml(out_xml)
            project.save()
        finally:
            project.close()
    a, b = instructions(src_xml), instructions(out_xml)
    print('instructions', len(a), len(b), 'identical:', a == b)


if __name__ == '__main__':
    main(*sys.argv[1:4])
