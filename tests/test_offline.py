"""Tests that need no GX Works3: type library parsing, struct codec, names."""

import struct

import pytest

from gxw3call import typelib
from gxw3call.client import DEFAULT_TLB, plc_file_entry
from gxw3call.naming import convert_keys, python_names, snake


@pytest.fixture(scope='module')
def tlb():
    return typelib.load(DEFAULT_TLB)


def test_interfaces_and_methods(tlb):
    counts = {
        name: len(iface.methods)
        for name, iface in tlb.interfaces.items()
        if iface.clsid
    }
    assert counts['IGXW3CallProject'] == 89
    assert counts['IGXW3CallCommunication'] == 163
    open_project = tlb.interfaces['IGXW3CallProject'].methods['OpenProject']
    assert open_project.vtbl == 10


def test_plc_file_entry_is_pack_1(tlb):
    # declared with natural alignment (568) but read with pack 1 (561)
    assert tlb.size_of('_ST_PLC_FILE_DATA') == 561
    entry = plc_file_entry(9, 'UNIT', 4, 1234)
    assert len(entry) == 561
    assert struct.unpack_from('<H', entry, 0)[0] == 9
    assert entry[2:10].decode('utf-16-le') == 'UNIT'
    assert struct.unpack_from('<i', entry, 516)[0] == 4
    assert struct.unpack_from('<Q', entry, 553)[0] == 1234


def test_every_struct_round_trips(tlb):
    for name in tlb.records:
        size = tlb.size_of(name)
        value = tlb.decode(name, bytes(size))
        assert tlb.encode(name, value) == bytes(size), name
        assert tlb.encode(name, convert_keys(value)) == bytes(size), name


def test_encode_fields_by_python_name(tlb):
    conn = tlb.decode('_ST_CONNECTION_INFO_ALL', bytes(1018))
    conn = convert_keys(conn)
    conn['connection_name'] = 'Conn1'
    common = conn['connection_info']['connection_common_data']
    common['ip_addr'] = [192, 168, 3, 39]
    raw = tlb.encode('_ST_CONNECTION_INFO_ALL', conn)
    back = tlb.decode('_ST_CONNECTION_INFO_ALL', raw)
    assert back['szConnectionName'] == 'Conn1'
    ip = back['stConnectionInfo']['stConnectionCommonData']['lIPAddr']
    assert ip == [192, 168, 3, 39]


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('GetPOUNames', 'get_pou_names'),
        ('IsPlcOnline', 'is_plc_online'),
        ('GetCCLinkConfig', 'get_cc_link_config'),
    ],
)
def test_snake(name, expected):
    assert snake(name) == expected


def test_python_names_drop_prefix_and_stay_unique():
    names = python_names(['lpbCallID', 'bstrProjectPath', 'lplReturnCode'])
    assert names == {
        'lpbCallID': 'call_id',
        'bstrProjectPath': 'project_path',
        'lplReturnCode': 'return_code',
    }
    clash = python_names(['lpdwSize', 'dwSize'])
    assert len(set(clash.values())) == 2
