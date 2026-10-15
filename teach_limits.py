#!/usr/bin/env python3
'''
Teach the safe area.

Procedure:
  1. Bring the mount into the home position (e.g. counterweight down, tube toward the pole),
     THEN power it on.
  2. Start telescope_server.py (except with --hootl).
  3. Start this script. The start position is saved as home.
  4. Drive ONCE ALL AROUND the boundary of the safe area, storing
     boundary points in order with "m". The points are connected into an
     outline in this order. Where the permitted range changes
     sharply, set more points.
  5. Optional: exclusion zones. These are areas INSIDE the outline that the
     mount must not enter (e.g. where the rear of a long tube hits the
     tripod or pier). "n" starts an exclusion zone, then drive along its boundary
     in order with "m", "e" ends it.
  6. Save with "s", quit with "q".

With --edit an existing file is loaded, e.g. to add exclusion zones later.

Keys:
  Arrow left/right    move axis 1 (RA)         while held down
  Arrow up/down       move axis 2 (Dec)        while held down
  Space               stop immediately
  + / -               change speed
  m                   store current position as boundary point (outline or current exclusion zone)
  u                   discard last boundary point
  n                   start new exclusion zone
  e                   end exclusion zone, back to the outline
  g                   save graphic of the outline as SVG file
  s                   save (also writes the graphic)
  q                   quit (motors are stopped)

Check mode (--check):
  Loads the saved outline from config.yaml (axis_limits) and enables
  monitoring as it later works in NexPlane. You can move freely
  and check whether the mount stops at the boundary. Storing points and saving are disabled.

In the terminal a small map shows the boundary points (1-9, A-Z), home (H)
and the current position (@). Edges that cross appear as "!",
the automatic closing line to point 1 as ":".
'''

import argparse
import curses
import math
import os
import time

from typing import Any

import axis_limits
import limits_plot
import config
import mount_base
import skywatcher

from axis_limits import deg, rad
from util import SKYWATCHER_TELESCOPE_PROTOCOLS

SPEEDS_DEG_S = [0.1, 0.25, 0.5, 1.0, 2.0, 3.5, 5.0]

def parse_args_and_config() -> tuple[argparse.Namespace, dict[str, Any]]:
    parser, config_data, validators = config.get_arg_parser_and_config_data(
        description='Teach the safe area for NexPlane (Sky-Watcher mounts only).')
    config.add_arg_hootl(parser, config_data, validators)
    config.add_arg_telescope(parser, config_data, validators)
    config.add_arg_telescope_protocol(parser, config_data, validators)

    settings = axis_limits.get_settings(config_data) or dict(axis_limits.DEFAULTS)
    parser.add_argument('--output', type=str, default=None,
                        help='Output file (default: axis_limits.file from config.yaml, or limits.yaml)')
    parser.add_argument('--edit', action='store_true',
                        help='Load an existing file and continue editing it (e.g. to add exclusion zones)')
    parser.add_argument('--check', action='store_true',
                        help='Check mode: load the saved outline and enable the limit guard')
    parser.add_argument('--deadman', type=float, default=0.8,
                        help='Seconds without a key press until the mount stops automatically (default: 0.8)')
    args = parser.parse_args()
    config.validate(validators, args)
    if args.telescope_protocol not in SKYWATCHER_TELESCOPE_PROTOCOLS:
        raise SystemExit('teach_limits.py only supports Sky-Watcher protocols.')
    if args.output is None:
        args.output = settings.get('file') or 'limits.yaml'
    args.margin = float(settings['margin_degrees'])
    return args, config_data

def open_mount(args: argparse.Namespace) -> tuple[mount_base.Client, skywatcher.SkyWatcher]:
    '''Connect to the mount (or the simulator) directly, without NexPlane.'''
    client: mount_base.Client
    if args.run_hootl:
        client = skywatcher.SkyWatcherSerialHootl()
    elif args.telescope_protocol == 'skywatcher-mount-head-wifi':
        client = skywatcher.SkyWatcherUdpClient(args.telescope)
    else:
        client = mount_base.SerialNetClient(args.telescope)
    return client, skywatcher.SkyWatcher(client)

class Teacher:
    '''Logic without screen, so that it is testable.'''
    def __init__(self, mount: skywatcher.SkyWatcher, output: str, deadman: float, margin_deg: float = 3.0):
        self.mount = mount
        self.output = output
        self.deadman = deadman
        self.margin = rad(margin_deg)
        self.speed_idx = 2
        self.cmd = {1: 0.0, 2: 0.0}
        self.last_key_time = {1: 0.0, 2: 0.0}
        self.pos = {1: mount._inquire_position(1), 2: mount._inquire_position(2)}
        self.home = dict(self.pos)
        self.boundary: list[tuple[float, float]] = []
        self.exclusions: list[list[tuple[float, float]]] = []
        self.editing: int | None = None     # Index of the exclusion zone being edited, None = outline
        self.preview_path = os.path.splitext(output)[0] + '_preview.svg'
        self.saved = True
        self.check_mode = False
        self.message = 'Home position taken. Drive along the boundary and mark points with "m".'

    @property
    def speed(self) -> float:
        return rad(SPEEDS_DEG_S[self.speed_idx])

    def jog(self, axis: int, direction: int) -> None:
        self.cmd[axis] = direction * self.speed
        self.last_key_time[axis] = time.monotonic()

    def stop(self) -> None:
        self.cmd = {1: 0.0, 2: 0.0}
        for axis in [1, 2]:
            self.mount._slew_axis(axis, 0.0)

    def faster(self, step: int) -> None:
        self.speed_idx = max(0, min(len(SPEEDS_DEG_S) - 1, self.speed_idx + step))
        for axis in [1, 2]:
            if self.cmd[axis] != 0:
                self.cmd[axis] = math.copysign(self.speed, self.cmd[axis])

    def _target(self) -> list[tuple[float, float]]:
        return self.boundary if self.editing is None else self.exclusions[self.editing]

    def target_name(self) -> str:
        return 'Outline' if self.editing is None else f'Exclusion zone {self.editing + 1}'

    def mark(self) -> None:
        target = self._target()
        target.append((self.pos[1], self.pos[2]))
        self.saved = False
        self.message = f'{self.target_name()}: point {len(target)} marked.'

    def undo(self) -> None:
        '''Discard the last point of the outline or zone being edited (an empty zone is removed).'''
        target = self._target()
        if target:
            target.pop()
            self.saved = False
            self.message = f'{self.target_name()}: last point discarded.'
        elif self.editing is not None:
            del self.exclusions[self.editing]
            self.editing = None
            self.saved = False
            self.message = 'Empty exclusion zone removed, back to the outline.'
        else:
            self.message = 'There are no boundary points.'

    def new_exclusion(self) -> None:
        if self.editing is not None and len(self.exclusions[self.editing]) < 3:
            self.message = f'{self.target_name()} has fewer than 3 points. Finish it first or clear it with "u".'
            return
        self.exclusions.append([])
        self.editing = len(self.exclusions) - 1
        self.message = (f'{self.target_name()} started: mark its boundary in order with "m", '
                        '"e" ends it.')

    def end_exclusion(self) -> None:
        if self.editing is None:
            self.message = 'No exclusion zone is being edited.'
            return
        zone = self.exclusions[self.editing]
        if 0 < len(zone) < 3:
            self.message = f'{self.target_name()} needs at least 3 points (or clear it with "u").'
            return
        if not zone:
            del self.exclusions[self.editing]
        self.editing = None
        self.message = 'Back to the outline.'

    def save(self) -> None:
        '''Save the limits file (validated by save_limits) and the SVG preview.'''
        try:
            axis_limits.save_limits(self.output, self.mount.cpr, self.home, self.boundary, self.exclusions)
            self.saved = True
            self.message = (f'Saved: {self.output} ({len(self.boundary)} boundary points, '
                            f'{len([z for z in self.exclusions if z])} exclusion zone(s))')
            ok = True
        except axis_limits.LimitError as e:
            self.message = f'Not saved: {e}'
            ok = False
        if self.boundary:
            message = self.message
            self.write_preview()
            self.message = message + (f' | Graphic: {os.path.basename(self.preview_path)}' if ok
                                      else f' See the graphic: {os.path.basename(self.preview_path)}')

    def write_preview(self) -> None:
        if not self.boundary:
            self.message = 'No boundary points for a graphic yet.'
            return
        status = self.problem() or 'Outline is valid.'
        limits_plot.write_svg(self.preview_path, (self.home[1], self.home[2]), self.boundary,
                              (self.pos[1], self.pos[2]), self.margin, status, self.exclusions)
        self.message = f'Graphic saved: {os.path.basename(self.preview_path)}'

    def update(self) -> None:
        '''Call periodically: check dead-man switch, send commands, read positions.'''
        now = time.monotonic()
        for axis in [1, 2]:
            if self.cmd[axis] != 0 and now - self.last_key_time[axis] > self.deadman:
                self.cmd[axis] = 0.0
            self.mount._slew_axis(axis, self.cmd[axis])
            self.pos[axis] = self.mount._inquire_position(axis)

    def problem(self) -> str | None:
        '''First problem of the taught outline and zones, or None if they are valid.'''
        problems = axis_limits.polygon_problems(self.boundary)
        if problems:
            return problems[0]
        home = (self.home[1], self.home[2])
        if not axis_limits.point_in_polygon(home, self.boundary):
            return 'Home lies outside the outline.'
        zones = [z for z in self.exclusions if len(z) >= 3]
        problems = axis_limits.exclusion_problems(self.boundary, zones, home)
        return problems[0] if problems else None

    def status_lines(self) -> list[str]:
        '''Status lines for the screen: what is edited, last point, validity, clearance here.'''
        lines = [f'Editing: {self.target_name()}   (exclusion zones: {len(self.exclusions)})']
        target = self._target()
        if target:
            last = target[-1]
            lines.append(f'Last point (rel. to home): axis 1 {deg(last[0] - self.home[1]):+.2f}, '
                         f'axis 2 {deg(last[1] - self.home[2]):+.2f} degrees')
        if len(self.boundary) >= 3:
            problem = self.problem()
            if problem:
                lines.append('Not valid like this: ' + problem)
            else:
                p = (self.pos[1], self.pos[2])
                c = axis_limits.clearance(p, self.boundary, self.margin,
                                          [z for z in self.exclusions if len(z) >= 3])
                state = 'inside' if c >= 0 else 'OUTSIDE'
                lines.append(f'Current position with {deg(self.margin):.1f} degrees margin: {state} '
                             f'({deg(c):+.2f} degrees)')
        return lines

def ui(stdscr: Any, teacher: Teacher) -> None:
    '''curses loop: keys -> Teacher actions, then redraw status and the ASCII map.'''
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    confirm_quit = False
    while True:
        while True:
            key = stdscr.getch()
            if key == -1:
                break
            if key == curses.KEY_LEFT:
                teacher.jog(1, -1)
            elif key == curses.KEY_RIGHT:
                teacher.jog(1, +1)
            elif key == curses.KEY_UP:
                teacher.jog(2, +1)
            elif key == curses.KEY_DOWN:
                teacher.jog(2, -1)
            elif key == ord(' '):
                teacher.stop()
                teacher.message = 'Stopped.'
            elif key in (ord('+'), ord('=')):
                teacher.faster(+1)
            elif key == ord('-'):
                teacher.faster(-1)
            elif key == ord('m'):
                if teacher.check_mode:
                    teacher.message = 'Not available in check mode.'
                else:
                    teacher.mark()
            elif key == ord('u'):
                if teacher.check_mode:
                    teacher.message = 'Not available in check mode.'
                else:
                    teacher.undo()
            elif key == ord('s'):
                if teacher.check_mode:
                    teacher.message = 'Not available in check mode.'
                else:
                    teacher.save()
            elif key in (ord('n'), ord('e')):
                if teacher.check_mode:
                    teacher.message = 'Not available in check mode.'
                elif key == ord('n'):
                    teacher.new_exclusion()
                else:
                    teacher.end_exclusion()
            elif key == ord('g'):
                teacher.write_preview()
            elif key == ord('q'):
                if teacher.saved or confirm_quit:
                    return
                confirm_quit = True
                teacher.message = 'Not saved! Press "q" again to discard, "s" to save.'
                continue
            confirm_quit = False

        try:
            teacher.update()
        except mount_base.CommError as e:
            teacher.message = f'Communication error: {e}'

        stdscr.erase()
        rows, cols = stdscr.getmaxyx()
        lines = [
            'NexPlane - ' + ('CHECK the safe area (limit guard active)' if teacher.check_mode else 'teach the safe area'),
            f'Axis 1 (RA):  {deg(teacher.pos[1] - teacher.home[1]):+9.2f} degrees   '
            f'command {deg(teacher.cmd[1]):+5.2f} degrees/s',
            f'Axis 2 (Dec): {deg(teacher.pos[2] - teacher.home[2]):+9.2f} degrees   '
            f'command {deg(teacher.cmd[2]):+5.2f} degrees/s',
            f'Speed: {SPEEDS_DEG_S[teacher.speed_idx]} degrees/s   '
            f'boundary points: {len(teacher.boundary)}',
            *teacher.status_lines(),
            'Hold arrows = move | Space = STOP | +/- speed',
            'm = mark point | u = discard | n/e = start/end exclusion zone | g = graphic | s = save | q = quit',
            teacher.message,
            '',
        ]
        map_h = rows - len(lines) - 1
        if map_h >= 8 and cols >= 30:
            lines += limits_plot.render_ascii((teacher.home[1], teacher.home[2]), teacher.boundary,
                                              (teacher.pos[1], teacher.pos[2]), cols - 1, map_h,
                                              teacher.exclusions)
        for i, line in enumerate(lines[:rows]):
            try:
                stdscr.addstr(i, 0, line[:cols - 1])
            except curses.error:
                pass
        stdscr.refresh()
        time.sleep(0.05)

def main() -> None:
    args, config_data = parse_args_and_config()
    output = axis_limits.resolve_path(args.output)
    client, mount = open_mount(args)
    teacher: Teacher | None = None
    try:
        guard = None
        if args.check:
            guard = axis_limits.install_from_config(mount, config_data, args.telescope_protocol)
            if guard is None:
                raise SystemExit('Check mode needs the axis_limits section in config.yaml.')
        teacher = Teacher(mount, output, args.deadman, args.margin)
        if guard is not None:
            teacher.check_mode = True
            teacher.boundary = list(guard.poly)
            teacher.exclusions = [list(z) for z in guard.holes]
            teacher.home = {1: guard.home[0], 2: guard.home[1]}
            teacher.margin = guard.margin
            teacher.message = 'CHECK MODE: limit guard active. Move freely and check the behaviour at the edge.'

            def notify(text: str, t: Teacher = teacher) -> None:
                t.message = f'CHECK MODE: {text}'
            guard.notify = notify
        elif args.edit:
            limits = axis_limits.load_limits(output)
            if int(limits['cpr'][1]) != mount.cpr[1] or int(limits['cpr'][2]) != mount.cpr[2]:
                raise SystemExit('The file does not match this mount (steps per revolution).')
            teacher.home = {1: rad(limits['home_deg'][1]), 2: rad(limits['home_deg'][2])}
            teacher.boundary = [(rad(a), rad(b)) for a, b in limits['polygon_deg']]
            teacher.exclusions = [[(rad(a), rad(b)) for a, b in z] for z in (limits.get('exclusions_deg') or [])]
            off = max(abs(teacher.pos[1] - teacher.home[1]), abs(teacher.pos[2] - teacher.home[2]))
            teacher.message = (f'{os.path.basename(output)} loaded: {len(teacher.boundary)} boundary points, '
                               f'{len(teacher.exclusions)} exclusion zone(s).')
            if off > rad(1.0):
                teacher.message += (f' WARNING: The mount is {deg(off):.1f} degrees away from the saved home '
                                    'position. Was it switched on at home?')
        curses.wrapper(ui, teacher)
    finally:
        for axis in [1, 2]:
            try:
                mount._slew_axis(axis, 0.0)
            except Exception:
                pass
        client.close()
    if teacher is not None and not teacher.check_mode and teacher.boundary and teacher.saved and os.path.exists(output):
        print('Safe area saved in', output)

if __name__ == '__main__':
    main()
