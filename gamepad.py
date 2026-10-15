#!/usr/bin/env python3
'''
Gamepad (e.g. PS4 controller via Bluetooth) as a control device for NexPlane.

Reads the controller directly through the Linux input devices (/dev/input/event*),
without any extra package. The browser window does not need to be in the foreground.

Test mode (shows detected devices and every button/axis):
    ./gamepad.py --test

If there is no access to /dev/input/event*:
    sudo usermod -aG input $USER     (then log out and back in)
'''

import argparse
import errno
import fcntl
import glob
import os
import select
import struct
import threading
import time

from typing import Any

from messages import Msg, Text

# --- Linux input constants (linux/input-event-codes.h) ----------------------
EV_SYN, EV_KEY, EV_ABS, EV_FF = 0x00, 0x01, 0x03, 0x15
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
FF_RUMBLE = 0x50

BUTTONS = {
    0x130: 'cross',      # BTN_SOUTH
    0x131: 'circle',     # BTN_EAST
    0x133: 'triangle',   # BTN_NORTH
    0x134: 'square',     # BTN_WEST
    0x136: 'l1',         # BTN_TL
    0x137: 'r1',         # BTN_TR
    0x138: 'l2_button',  # BTN_TL2
    0x139: 'r2_button',  # BTN_TR2
    0x13a: 'share',      # BTN_SELECT
    0x13b: 'options',    # BTN_START
    0x13c: 'ps',         # BTN_MODE
    0x13d: 'l3',         # BTN_THUMBL
    0x13e: 'r3',         # BTN_THUMBR
}
AXES = {ABS_X: 'lx', ABS_Y: 'ly', ABS_RX: 'rx', ABS_RY: 'ry', ABS_Z: 'l2', ABS_RZ: 'r2'}
TRIGGERS = ('l2', 'r2')

# struct input_event: struct timeval (2 x long), __u16 type, __u16 code, __s32 value
EVENT_FMT = 'llHHi'
EVENT_SIZE = struct.calcsize(EVENT_FMT)

def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord('E') << 8) | nr

def _eviocgabs(axis: int) -> int:
    return _ioc(2, 0x40 + axis, 24)          # _IOR('E', 0x40 + abs, struct input_absinfo)

FF_EFFECT_SIZE = 48                          # struct ff_effect on 64-bit Linux
EVIOCSFF = _ioc(1, 0x80, FF_EFFECT_SIZE)     # _IOW('E', 0x80, struct ff_effect)

# --- Device discovery ----------------------------------------------------------

def _caps(event_dir: str, kind: str) -> int:
    '''Capability bitmask of an input device (from /sys/class/input/eventN/device/capabilities).'''
    try:
        with open(os.path.join(event_dir, 'device', 'capabilities', kind)) as f:
            words = f.read().split()
    except OSError:
        return 0
    value = 0
    for w in words:
        value = (value << 64) | int(w, 16)
    return value

def list_devices() -> list[dict[str, Any]]:
    '''All input devices with their name and whether they look like a gamepad.'''
    out = []
    for event_dir in sorted(glob.glob('/sys/class/input/event*'), key=lambda p: int(p.rsplit('event', 1)[1])):
        try:
            with open(os.path.join(event_dir, 'device', 'name')) as f:
                name = f.read().strip()
        except OSError:
            continue
        keys = _caps(event_dir, 'key')
        absbits = _caps(event_dir, 'abs')
        is_pad = bool(keys >> 0x130 & 1) and bool(absbits >> ABS_X & 1) and bool(absbits >> ABS_Y & 1)
        out.append({'path': '/dev/input/' + os.path.basename(event_dir), 'name': name, 'gamepad': is_pad})
    return out

def find_device(name_filter: str = '') -> dict[str, Any] | None:
    '''First gamepad whose name contains name_filter (any gamepad if empty), or None.'''
    for dev in list_devices():
        if not dev['gamepad']:
            continue
        if name_filter and name_filter.lower() not in dev['name'].lower():
            continue
        return dev
    return None

# --- Reader --------------------------------------------------------------------

class Gamepad:
    '''
    Reads the controller in a background thread. Queries from the main loop:
      axes()          -> {'lx','ly','rx','ry': -1..1 (dead zone applied, y up is positive),
                          'l2','r2': 0..1}
      pop_presses()   -> list of buttons pressed since the last call
                         (names from BUTTONS plus 'up', 'down', 'left', 'right' for the D-pad)
      connected, name, error
    After a disconnect (Bluetooth) the device is searched for again automatically.
    '''
    def __init__(self, name_filter: str = '', deadzone: float = 0.12, rumble: bool = True,
                 path: str | None = None):
        self.name_filter = name_filter
        self.deadzone = deadzone
        self.rumble_enabled = rumble
        self.fixed_path = path
        self.lock = threading.Lock()
        self.raw: dict[str, float] = {k: 0.0 for k in AXES.values()}
        self.ranges: dict[int, tuple[int, int]] = {}
        self.presses: list[str] = []
        self.hat = {ABS_HAT0X: 0, ABS_HAT0Y: 0}
        self.connected = False
        self.name = ''
        self.error: Text = ''
        self.fd: int | None = None
        self.ff_ids: dict[tuple[int, int, int], int] = {}
        self.stop_flag = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # --- Queries ---

    def _shape(self, v: float) -> float:
        a = abs(v)
        if a <= self.deadzone:
            return 0.0
        return (a - self.deadzone) / (1.0 - self.deadzone) * (1 if v > 0 else -1)

    def axes(self) -> dict[str, float]:
        '''Current stick and trigger positions (see the class docstring); all 0 while disconnected.'''
        with self.lock:
            if not self.connected:
                return {k: 0.0 for k in AXES.values()}
            r = dict(self.raw)
        out = {k: self._shape(r[k]) for k in ('lx', 'ly', 'rx', 'ry')}
        out['ly'] = -out['ly']      # stick up = positive
        out['ry'] = -out['ry']
        for k in TRIGGERS:
            out[k] = max(0.0, min(1.0, r[k]))
        return out

    def pop_presses(self) -> list[str]:
        with self.lock:
            p, self.presses = self.presses, []
            return p

    def close(self) -> None:
        self.stop_flag = True
        self._close_fd()

    # --- Vibration (feedback when not looking at the screen) ---

    def rumble(self, strong: float = 0.6, weak: float = 0.6, ms: int = 120, count: int = 1) -> None:
        if not self.rumble_enabled or count < 1:
            return
        threading.Thread(target=self._rumble, args=(strong, weak, ms, count), daemon=True).start()

    def _rumble(self, strong: float, weak: float, ms: int, count: int) -> None:
        '''Play a rumble effect count times (runs in its own thread).'''
        for i in range(count):
            fd = self.fd
            if fd is None:
                return
            try:
                key = (int(strong * 0xFFFF), int(weak * 0xFFFF), ms)
                eid = self.ff_ids.get(key)
                if eid is None:
                    buf = bytearray(FF_EFFECT_SIZE)
                    # type, id(-1 = new), direction, trigger(button, interval), replay(length, delay)
                    struct.pack_into('HhHHHHH', buf, 0, FF_RUMBLE, -1, 0, 0, 0, ms, 0)
                    struct.pack_into('HH', buf, 16, key[0], key[1])      # union: strong, weak magnitude
                    fcntl.ioctl(fd, EVIOCSFF, buf, True)
                    eid = struct.unpack_from('h', buf, 2)[0]
                    self.ff_ids[key] = eid
                os.write(fd, struct.pack(EVENT_FMT, 0, 0, EV_FF, eid, 1))
            except OSError:
                self.rumble_enabled = False     # device cannot vibrate; continue silently
                return
            time.sleep(ms / 1000 + 0.08)

    # --- Thread ---

    def _close_fd(self) -> None:
        '''Close the device after an error or disconnect; axes return to 0.'''
        fd, self.fd = self.fd, None
        self.ff_ids = {}
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        with self.lock:
            self.connected = False
            self.raw = {k: 0.0 for k in AXES.values()}

    def _open(self) -> bool:
        '''Open the gamepad and read its axis ranges. Returns False (and sets error) if there is none.'''
        if self.fixed_path:
            dev: dict[str, Any] = {'path': self.fixed_path, 'name': self.fixed_path}
        else:
            found = find_device(self.name_filter)
            if found is None:
                self.error = Msg('No gamepad found (Bluetooth connected?)')
                return False
            dev = found
        try:
            try:
                fd = os.open(dev['path'], os.O_RDWR | os.O_NONBLOCK)
            except OSError:
                fd = os.open(dev['path'], os.O_RDONLY | os.O_NONBLOCK)
        except PermissionError:
            self.error = Msg('No access to {path}: run "sudo usermod -aG input $USER" and log in again.',
                             path=dev['path'])
            return False
        except OSError as e:
            self.error = f'{dev["path"]}: {e}'
            return False
        self.ranges = {}
        for code in list(AXES) + [ABS_HAT0X, ABS_HAT0Y]:
            try:
                buf = bytearray(24)
                fcntl.ioctl(fd, _eviocgabs(code), buf, True)
                value, lo, hi = struct.unpack_from('iii', buf, 0)
                if hi > lo:
                    self.ranges[code] = (lo, hi)
                    self._set_abs(code, value)
            except OSError:
                pass
        self.fd = fd
        self.name = dev['name']
        self.error = ''
        with self.lock:
            self.connected = True
        return True

    def _norm(self, code: int, value: int) -> float:
        lo, hi = self.ranges.get(code, (0, 255))
        if AXES.get(code) in TRIGGERS:
            return (value - lo) / (hi - lo)
        return (2.0 * (value - lo) / (hi - lo)) - 1.0

    def _set_abs(self, code: int, value: int) -> None:
        '''Store an axis value; a D-pad (hat) change becomes a button press.'''
        if code in AXES:
            with self.lock:
                self.raw[AXES[code]] = self._norm(code, value)
        elif code in self.hat:
            old = self.hat[code]
            self.hat[code] = value
            if value != 0 and value != old:
                if code == ABS_HAT0X:
                    name = 'right' if value > 0 else 'left'
                else:
                    name = 'down' if value > 0 else 'up'
                with self.lock:
                    self.presses.append(name)

    def handle(self, etype: int, code: int, value: int) -> None:
        if etype == EV_KEY and value == 1 and code in BUTTONS:
            with self.lock:
                self.presses.append(BUTTONS[code])
        elif etype == EV_ABS:
            self._set_abs(code, value)

    def _run(self) -> None:
        '''Background thread: read input events, reconnect after errors.'''
        while not self.stop_flag:
            if self.fd is None:
                if not self._open():
                    time.sleep(2.0)
                    continue
            fd = self.fd
            assert fd is not None
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    continue
                data = os.read(fd, EVENT_SIZE * 64)
                if not data:
                    raise OSError(errno.ENODEV, 'device disconnected')
                for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
                    self.handle(etype, code, value)
            except BlockingIOError:
                continue
            except (OSError, ValueError) as e:
                self.error = Msg('Connection to the gamepad lost ({error}); searching again ...', error=str(e))
                self._close_fd()
                time.sleep(1.0)

def main() -> None:
    '''Test mode: list input devices, then show buttons and axes live.'''
    parser = argparse.ArgumentParser(description='Test a gamepad: shows input devices, buttons and axes.')
    parser.add_argument('--test', action='store_true', help='Show buttons and axes live')
    parser.add_argument('--name', default='', help='Only devices whose name contains this text')
    parser.add_argument('--device', default=None, help='Device file to use, e.g. /dev/input/event17')
    args = parser.parse_args()

    print('Input devices:')
    for dev in list_devices():
        print(f'  {dev["path"]:22s} {"GAMEPAD " if dev["gamepad"] else "        "} {dev["name"]}')
    if not args.test:
        print('\nWith --test, buttons and axes are shown live.')
        return
    pad = Gamepad(args.name, path=args.device)
    print('\nWaiting for a gamepad ... (Ctrl+C quits). Every button press vibrates briefly.')
    last_err = ''
    shown_name = ''
    try:
        while True:
            time.sleep(0.1)
            err = str(pad.error)
            if err and err != last_err:
                print('\n' + err)
                last_err = err
            if pad.connected and pad.name != shown_name:
                print(f'\nConnected: {pad.name}')
                shown_name = pad.name
            for p in pad.pop_presses():
                print(f'\nButton: {p}')
                pad.rumble(ms=80)
            a = pad.axes()
            print('\r' + '  '.join(f'{k} {a[k]:+.2f}' for k in ('lx', 'ly', 'rx', 'ry', 'l2', 'r2')) + '   ',
                  end='', flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        pad.close()

if __name__ == '__main__':
    main()
