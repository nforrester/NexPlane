#!/usr/bin/env python

'''This script helps run a library of useful testcases to exercise NexPlane in HOOTL.'''

import itertools
import os
import subprocess
import sys
import tempfile
import time

from typing import Any, Iterator
from contextlib import contextmanager

from util import assert_int

LOCATION = 'griffith'
LANDMARK = 'hollywood_sign'
SAT_PORT = '40004'
SCOPE_PORT = '45345'

@contextmanager
def bg(cmd: list[str]) -> Iterator:
    print(' '.join(cmd))
    proc = subprocess.Popen(cmd)
    try:
        yield
    finally:
        proc.terminate()
        proc.wait()

def fg(cmd: list[str]) -> None:
    print(' '.join(cmd))
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        print('Test FAILED!')
        sys.exit(1)

def run_mypy() -> None:
    fg([
        'mypy',
        'align.py',
        'dump_config.py',
        'nexplane.py',
        'rpc_client_test.py',
        'rpc_server_test.py',
        'run_test.py',
        'satellites.py',
        'skywatcher_wifi_hootl.py',
        'telescope_server.py',
    ])

def satellites() -> Any:
    return bg(['./satellites.py', 'tle/geo.txt', '--location', LOCATION, '--port', SAT_PORT])

def telescope_server(telescope_protocol: str, mount_mode: str) -> Any:
    return bg([
        './telescope_server.py',
        '--hootl',
        '--location', LOCATION,
        '--network-port', SCOPE_PORT,
        '--telescope-protocol', telescope_protocol,
        '--mount-mode', mount_mode,
    ])

def align(alignment: str, telescope_protocol: str, mount_mode: str) -> Any:
    return fg([
        './align.py',
        '--location', LOCATION,
        '--landmark', LANDMARK,
        '--telescope-protocol', telescope_protocol,
        '--mount-mode', mount_mode,
        '--alignment', alignment,
    ])

def nexplane_flexible(hootl: bool, landmark: bool, alignment: str | None, telescope_protocol: str, mount_mode: str, *args: str) -> None:
    cmd = []
    cmd.append('./nexplane.py')
    cmd.append('--hootl' if hootl else '--no-hootl')
    cmd.extend(['--location', LOCATION])
    if landmark:
        cmd.extend(['--landmark', LANDMARK])
    if alignment is not None:
        cmd.extend(['--alignment', alignment])
    cmd.extend(['--sbs1', 'localhost:'+SAT_PORT])
    cmd.extend(['--telescope-protocol', telescope_protocol])
    cmd.extend(['--mount-mode', mount_mode])
    cmd.extend(args)
    fg(cmd)

def nexplane(*args: str) -> None:
    with satellites():
        time.sleep(0.1)
        nexplane_flexible(True, True, None, *args)

def nexplane_with_server(telescope_protocol: str, mount_mode: str, *args: str) -> None:
    with satellites():
        with telescope_server(telescope_protocol, mount_mode):
            time.sleep(0.1)
            nexplane_flexible(False, True, None, telescope_protocol, mount_mode, '--telescope', 'localhost:'+SCOPE_PORT, *args)

def nexplane_with_skywatcher_wifi(mount_mode: str, *args: str) -> None:
    with satellites():
        with bg(['./skywatcher_wifi_hootl.py', SCOPE_PORT]):
            time.sleep(0.1)
            nexplane_flexible(False, True, None, 'skywatcher-mount-head-wifi', mount_mode, '--telescope', 'localhost:'+SCOPE_PORT, *args)

def nexplane_with_align(telescope_protocol: str, mount_mode: str, *args: str) -> None:
    with tempfile.TemporaryDirectory() as tempdir:
        alignment = os.path.join(tempdir, 'align.yaml')

        with satellites():
            with telescope_server(telescope_protocol, mount_mode):
                time.sleep(0.1)
                align(alignment, telescope_protocol, mount_mode)
                nexplane_flexible(False, False, alignment, telescope_protocol, mount_mode, '--telescope', 'localhost:'+SCOPE_PORT, *args)

def test(test_num: int, description: Any, function: Any, *args: Any, **kwargs: Any) -> None:
    print()
    print()
    print(f'TEST {test_num}: {description}')
    function(*args, **kwargs)

TESTS = [
    [0, 'MyPy', run_mypy],

    [1, 'NexStar mount in Alt-Az mode',
     nexplane, 'nexstar-hand-control', 'altaz'],

    [2, 'NexStar mount in Equatorial mode',
     nexplane, 'nexstar-hand-control', 'eq'],

    [3, 'Sky-Watcher mount with USB in Alt-Az mode',
     nexplane, 'skywatcher-mount-head-usb', 'altaz'],

    [4, 'Sky-Watcher mount with USB in Equatorial mode, Black and white',
     nexplane, 'skywatcher-mount-head-usb', 'eq', '--bw'],

    [5, 'Sky-Watcher mount with EQMOD, White and black',
     nexplane, 'skywatcher-mount-head-eqmod', 'eq', '--bw', '--white-bg'],

    [6, 'Sky-Watcher mount with WiFi, White and color',
     nexplane, 'skywatcher-mount-head-wifi', 'eq', '--white-bg'],

    [7, 'Server mode - NexStar mount',
     nexplane_with_server, 'nexstar-hand-control', 'altaz'],

    [8, 'Server mode - Sky-Watcher mount with USB',
     nexplane_with_server, 'skywatcher-mount-head-usb', 'altaz'],

    [9, 'Server mode - Sky-Watcher mount with EQMOD',
     nexplane_with_server, 'skywatcher-mount-head-eqmod', 'altaz'],

    [10, 'Server mode - Sky-Watcher mount with WiFi',
     nexplane_with_skywatcher_wifi, 'altaz'],

    [11, 'Server mode, Alignment test - NexStar mount with USB in Alt-Az mode',
     nexplane_with_align, 'nexstar-hand-control', 'altaz'],

    [12, 'Server mode, Alignment test - Sky-Watcher mount with USB in Equatorial mode',
     nexplane_with_align, 'skywatcher-mount-head-usb', 'eq'],
]

def main() -> None:
    tests_to_run = set(map(int, sys.argv[1:]))
    for test_spec in TESTS:
        n = assert_int(test_spec[0])
        if tests_to_run and n not in tests_to_run:
            continue
        test(n, *test_spec[1:])

if __name__ == '__main__':
    main()
