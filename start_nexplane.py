#!/usr/bin/env python3
'''
Starts everything NexPlane needs, each in its own GNOME Terminal window:

  1. dump1090          (aircraft data from the RTL stick, port 30003)
  2. telescope_server  (connection to the mount, UDP port 45345)
  3. satellites.py     (only if requested, port 40004)
  4. nexplane.py --web (browser UI; alignment always in the browser)

The order matters: NexPlane connects to the data sources only once at
startup. Therefore the script waits until dump1090 (and satellites.py, if used)
are reachable before starting NexPlane.

Usage:
    ./start_nexplane.py            normal start
    ./start_nexplane.py --hootl    trial run with simulated mount

The programs run with the same Python as this script. dump1090 is taken from the
config key "dump1090" (path of the executable, default: dump1090 on the PATH) or from
--dump1090; the mount protocol is the usual telescope_protocol setting.

A program that is already running (port in use) is not started a second time.
To quit: exit NexPlane (button in the browser, Ctrl+C or closing its window). The
helper windows started by this script (dump1090, telescope_server, satellites)
then close automatically. Programs that were already running before are left alone.
If NexPlane ends with an error, its own window stays open so the message can be read.
'''

import argparse
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time

from typing import Any

import config

NEXPLANE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

PORT_DUMP1090 = 30003
PORT_TELESCOPE = 45345
PORT_SATELLITES = 40004

TLE_FILES = ['tle/visual.txt', 'tle/stations.txt']
TLE_MAX_AGE_DAYS = 2.0

def port_open(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False

def udp_port_in_use(port: int) -> bool:
    '''
    telescope_server listens via UDP (not TCP), so there is no connection to test. Instead, try to
    bind the port: if that fails, telescope_server (or another program) is using it.
    '''
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(('0.0.0.0', port))
        return False
    except OSError:
        return True
    finally:
        sock.close()

def wait_for_port(port: int, name: str, timeout_s: float, udp: bool = False) -> bool:
    '''Wait until a program listens on the port; prints progress. False after timeout_s.'''
    print(f'  waiting for {name} (port {port}) ', end='', flush=True)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if (udp_port_in_use(port) if udp else port_open(port)):
            print('ready')
            return True
        print('.', end='', flush=True)
        time.sleep(1)
    print(' no answer')
    return False

RUN_DIR = os.path.join(os.environ.get('XDG_RUNTIME_DIR') or '/tmp', f'nexplane-{os.getuid()}')

def prepare_run_dir() -> None:
    '''Directory for the PID files of the helper windows started by this script.'''
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
    for name in os.listdir(RUN_DIR):
        if name.endswith('.pid'):
            try:
                os.remove(os.path.join(RUN_DIR, name))
            except OSError:
                pass

def open_terminal(title: str, workdir: str, command: str, pid_name: str | None = None) -> None:
    '''
    Command in a new GNOME Terminal window; the window stays open after it finishes (to read errors).
    With pid_name the window's shell writes its PID to RUN_DIR, so that the NexPlane window can
    close it (together with the program) when NexPlane ends.
    '''
    pid_part = f'echo $$ > {shlex.quote(os.path.join(RUN_DIR, pid_name + ".pid"))}; ' if pid_name else ''
    script = (f'{pid_part}cd {shlex.quote(workdir)} && {command}; '
              'status=$?; echo; echo "--- finished (status $status) ---"; '
              'read -r -p "Press Enter to close this window." _')
    subprocess.Popen(['gnome-terminal', f'--title={title}', '--', 'bash', '-c', script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def open_nexplane_terminal(workdir: str, command: str) -> None:
    '''
    NexPlane in its own window. When NexPlane ends (normally, Ctrl+C or window closed), the
    helper windows listed in RUN_DIR are closed: each window shell is the leader of its own
    process group, so signalling the group ends the program and the shell, and the window closes.
    After a normal end the NexPlane window closes as well; after an error it stays open.
    '''
    run = shlex.quote(RUN_DIR)
    script = (
        'cleanup() { '
        f'  for f in {run}/*.pid; do '
        '    [ -e "$f" ] || continue; pid=$(cat "$f"); rm -f "$f"; '
        '    [ -n "$pid" ] || continue; '
        '    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null; '
        '    for i in 1 2 3 4 5 6; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done; '
        '    kill -KILL -- "-$pid" 2>/dev/null; '
        '  done; '
        '}; '
        'trap cleanup EXIT; trap "exit 129" HUP; trap "exit 143" TERM; '
        f'cd {shlex.quote(workdir)} && {command}; '
        'status=$?; '
        'echo; echo "--- NexPlane finished (status $status), closing the helper windows ---"; cleanup; '
        'if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then read -r -p "Press Enter to close this window." _; fi'
    )
    subprocess.Popen(['gnome-terminal', '--title=NexPlane', '--', 'bash', '-c', script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def ask_yes_no(question: str, default: bool = False) -> bool:
    '''Ask a yes/no question on the terminal.'''
    hint = '[Y/n]' if default else '[y/N]'
    while True:
        answer = input(f'{question} {hint} ').strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False

def tle_outdated() -> bool:
    '''True if a TLE file for satellites.py is missing or older than TLE_MAX_AGE_DAYS.'''
    for rel in TLE_FILES:
        path = os.path.join(NEXPLANE_DIR, rel)
        if not os.path.exists(path):
            return True
        if (time.time() - os.path.getmtime(path)) / 86400 > TLE_MAX_AGE_DAYS:
            return True
    return False

def check_prerequisites(dump1090: str | None) -> list[str]:
    '''Problems that prevent the start (missing programs or files).'''
    problems = []
    if shutil.which('gnome-terminal') is None:
        problems.append('gnome-terminal not found.')
    if dump1090 is None:
        problems.append('dump1090 not found (set "dump1090: /path/to/dump1090" in config.yaml or use --dump1090).')
    for f in ('nexplane.py', 'telescope_server.py', 'satellites.py', 'webgui.py'):
        if not os.path.exists(os.path.join(NEXPLANE_DIR, f)):
            problems.append(f'{f} is missing in {NEXPLANE_DIR}')
    return problems

def parse_args_and_config() -> tuple[argparse.Namespace, dict[str, Any]]:
    parser, config_data, validators = config.get_arg_parser_and_config_data(
        description='Starts dump1090, telescope_server and NexPlane (browser GUI).')
    config.add_arg_telescope_protocol(parser, config_data, validators)
    parser.add_argument('--hootl', action='store_true', help='Trial run with a simulated mount')
    parser.add_argument('--dump1090', default=str(config_data.get('dump1090', 'dump1090')),
                        help='Path of the dump1090 executable (default from config.yaml: dump1090, '
                             'otherwise dump1090 on the PATH)')
    args = parser.parse_args()
    config.validate(validators, args)
    return args, config_data

def main() -> None:
    args, config_data = parse_args_and_config()
    dump1090 = shutil.which(args.dump1090)
    web_port = int(config_data.get('web_port', 8765))

    problems = check_prerequisites(dump1090)
    if problems:
        print('Cannot start:')
        for p in problems:
            print(' -', p)
        sys.exit(1)

    prepare_run_dir()
    print('Starting NexPlane' + (' (trial run with a simulated mount)' if args.hootl else ''))
    print()
    with_satellites = ask_yes_no('Show satellites?', default=False)
    if not args.hootl:
        input('Mount switched on at the home position and connected by USB? Then press Enter. ')
    print()

    # 1) dump1090
    if port_open(PORT_DUMP1090):
        print('dump1090 is already running.')
    else:
        print('Starting dump1090 ...')
        assert dump1090 is not None
        open_terminal('dump1090', os.path.dirname(os.path.abspath(dump1090)),
                      f'{shlex.quote(dump1090)} --interactive --net', 'dump1090')
        if not wait_for_port(PORT_DUMP1090, 'dump1090', 20):
            print('dump1090 is not reachable. Is the RTL stick plugged in? Errors are shown in the dump1090 window.')
            sys.exit(1)

    # 2) telescope_server (not needed with --hootl: NexPlane simulates the mount itself)
    if not args.hootl:
        if udp_port_in_use(PORT_TELESCOPE):
            print('telescope_server is already running.')
        else:
            print('Starting telescope_server ...')
            open_terminal('telescope_server', NEXPLANE_DIR,
                          f'{shlex.quote(PYTHON)} telescope_server.py --telescope-protocol '
                          f'{shlex.quote(args.telescope_protocol)}',
                          'telescope_server')
            if not wait_for_port(PORT_TELESCOPE, 'telescope_server', 20, udp=True):
                print('telescope_server is not reachable. Is the mount switched on and connected by USB? '
                      'Errors are shown in the telescope_server window.')
                sys.exit(1)

    # 3) satellites.py (optional)
    sbs1 = [f'localhost:{PORT_DUMP1090}']
    if with_satellites:
        if port_open(PORT_SATELLITES):
            print('satellites.py is already running.')
            sbs1.append(f'localhost:{PORT_SATELLITES}')
        else:
            command = f'{shlex.quote(PYTHON)} satellites.py'
            if tle_outdated():
                print('Satellite data is missing or older than 2 days, it is downloaded first.')
                command = '(cd tle && ./fetch.sh); ' + command
            print('Starting satellites.py ...')
            open_terminal('satellites', NEXPLANE_DIR, command, 'satellites')
            if wait_for_port(PORT_SATELLITES, 'satellites.py', 60):
                sbs1.append(f'localhost:{PORT_SATELLITES}')
            else:
                print('satellites.py is not reachable, NexPlane starts without satellites.')

    # 4) NexPlane with browser UI, without saved alignment
    nexplane_args = ['--web']
    for server in sbs1:
        nexplane_args += ['--sbs1', server]
    if args.hootl:
        nexplane_args.append('--hootl')
    print('Starting NexPlane ...')
    open_nexplane_terminal(NEXPLANE_DIR,
                           f'{shlex.quote(PYTHON)} nexplane.py ' + ' '.join(shlex.quote(a) for a in nexplane_args))
    print()
    print(f'Done. The browser opens shortly (http://127.0.0.1:{web_port}/).')
    print('When NexPlane ends, the helper windows started here close automatically.')

if __name__ == '__main__':
    main()
