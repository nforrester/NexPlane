#!/usr/bin/env python3
'''
Connection test for the Sky-Watcher control box, without telescope_server and NexPlane.

Stop telescope_server and NexPlane first (otherwise they occupy the port).
    ./mount_check.py              (port chosen automatically, like telescope_server)
    ./mount_check.py /dev/ttyUSB1

Shows all USB serial ports and who currently has them open, and sends the box
the version query (:e1) 20 times. Run once with and once without a connected PS4 controller
and compare both outputs.
'''

import glob
import os
import sys
import time

import serial

def usb_info(tty: str) -> str:
    '''Driver and USB vendor/product of a serial port.'''
    name = os.path.basename(tty)
    dev = os.path.realpath(f'/sys/class/tty/{name}/device')
    parts = []
    try:
        parts.append('driver ' + os.path.basename(os.path.realpath(dev + '/driver')))
    except OSError:
        pass
    d = dev
    for _ in range(5):
        if os.path.exists(d + '/idVendor'):
            def rd(f: str) -> str:
                try:
                    with open(d + '/' + f) as fh:
                        return fh.read().strip()
                except OSError:
                    return '?'
            parts.append(f'USB {rd("idVendor")}:{rd("idProduct")} {rd("manufacturer")} {rd("product")}')
            break
        d = os.path.dirname(d)
    return ', '.join(parts)

def holders(path: str) -> list[str]:
    '''Processes that have the device open.'''
    out = []
    real = os.path.realpath(path)
    for fd in glob.glob('/proc/[0-9]*/fd/*'):
        try:
            if os.path.realpath(fd) == real:
                pid = fd.split('/')[2]
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmd = f.read().replace(b'\0', b' ').decode(errors='ignore').strip()
                out.append(f'PID {pid}: {cmd[:100]}')
        except OSError:
            continue
    return sorted(set(out))

def main() -> None:
    ports = sorted(glob.glob('/dev/ttyUSB*')) + sorted(glob.glob('/dev/ttyACM*')) + sorted(glob.glob('/dev/rfcomm*'))
    print('Serial ports:')
    for p in ports:
        h = holders(p)
        print(f'  {p:15s} {usb_info(p)}')
        for x in h:
            print(f'      opened by {x}')
    if not ports:
        print('  none')
    pads = []
    for ev in sorted(glob.glob('/sys/class/input/event*')):
        try:
            with open(ev + '/device/name') as f:
                n = f.read().strip()
            if 'controller' in n.lower() or 'sony' in n.lower() or 'playstation' in n.lower():
                pads.append(f'{os.path.basename(ev)}: {n}')
        except OSError:
            pass
    print('Game controller input devices:', ', '.join(pads) if pads else 'none')

    port = sys.argv[1] if len(sys.argv) > 1 else next((p for p in [f'/dev/ttyUSB{i}' for i in range(10)] if os.path.exists(p)), None)
    if port is None:
        print('\nNo /dev/ttyUSB* found (telescope_server searches the same way).')
        return
    print(f'\nTesting {port} at 115200 baud ...')
    try:
        s = serial.Serial(port=port, baudrate=115200, timeout=0)
    except Exception as e:
        print(f'  Cannot open {port}: {e}')
        return
    ok = 0
    for i in range(20):
        s.reset_input_buffer()
        t0 = time.monotonic()
        s.write(b':e1\r')
        resp = b''
        while time.monotonic() - t0 < 0.5 and not resp.endswith(b'\r'):
            resp += s.read(64)
        dt = (time.monotonic() - t0) * 1000
        good = resp.startswith(b'=') and resp.endswith(b'\r')
        ok += good
        print(f'  {i + 1:2d}: {"ok  " if good else "FAIL"} {dt:6.1f} ms  response {resp!r}')
        time.sleep(0.1)
    s.close()
    print(f'\n{ok} of 20 queries answered.')

if __name__ == '__main__':
    main()
