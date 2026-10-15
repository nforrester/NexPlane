'''
Safe area (soft limits) for Sky-Watcher mounts in NexPlane.

The safe area is an outline (polygon) in the two raw axis angles
(axis 1 = RA, axis 2 = Dec). It is taught with teach_limits.py by driving
along the boundary of the safe area in order and storing points.
This allows the permitted range of one axis to depend on the position of the
other axis.

The angles are step counter values relative to the mount's power-on state.
The outline is therefore only valid if the mount is in the same home position
every time it is powered on and the setup is unchanged.

This is a software protection feature. It does not replace supervision or
a reachable emergency stop (power plug).
'''

import datetime
import math
import os
import shutil
import time

import yaml

from typing import Any, Callable

import config
import skywatcher

from messages import Msg, Text, join

DEFAULTS: dict[str, Any] = {
    'file': None,
    'margin_degrees': 3.0,
    'lookahead_seconds': 1.0,
    'require_home_at_start': True,
    'home_tolerance_degrees': 1.0,
    'wrong_way_trip_degrees': 0.5,
}

Point = tuple[float, float]

class LimitError(Exception):
    '''Limits violated, implausible or not loadable. Causes the program to abort.'''
    pass

def rad(deg: float) -> float:
    return deg / 180.0 * math.pi

def deg(r: float) -> float:
    return r * 180.0 / math.pi

def resolve_path(path: str) -> str:
    '''Relative paths are relative to the NexPlane directory.'''
    if os.path.isabs(path):
        return path
    return os.path.join(config.main_config_dir(), path)

def get_settings(config_data: dict[str, Any]) -> dict[str, Any] | None:
    '''Reads the axis_limits section from the configuration (or None).'''
    section = config_data.get('axis_limits')
    if section is None:
        return None
    settings = dict(DEFAULTS)
    settings.update(section)
    # Profiles: several taught areas, e.g. for different setups
    # (long tube / camera only). Without profiles, file is the only profile.
    profiles = settings.get('profiles') or {}
    if not isinstance(profiles, dict):
        raise LimitError(Msg('axis_limits.profiles must map profile names to file names.'))
    if not profiles and settings.get('file'):
        profiles = {'default': settings['file']}
    settings['profiles'] = dict(profiles)
    if profiles:
        active = settings.get('profile') or next(iter(profiles))
        if active not in profiles:
            raise LimitError(Msg('axis_limits.profile "{name}" does not exist. Available: {names}',
                                 name=active, names=', '.join(profiles)))
        settings['profile'] = active
        settings['file'] = profiles[active]
    return settings

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    '''Ray casting. Points exactly on the boundary do not reliably count as inside.'''
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside

def segment_distance(p: Point, a: Point, b: Point) -> float:
    '''Distance from point p to the segment a-b.'''
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

def edge_distance(p: Point, poly: list[Point]) -> float:
    n = len(poly)
    return min(segment_distance(p, poly[i], poly[(i + 1) % n]) for i in range(n))

def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)

def crossing_edges(poly: list[Point]) -> list[tuple[int, int]]:
    '''
    Pairs of crossing edges. Edge i connects point i with point i+1,
    the last edge (n-1) is the automatic closing line back to point 0.
    '''
    n = len(poly)
    pairs: list[tuple[int, int]] = []
    if n < 4:
        return pairs
    for i in range(n):
        for j in range(i + 1, n):
            # Adjacent edges share a point and do not count.
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if _segments_cross(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n]):
                pairs.append((i, j))
    return pairs

def polygon_problems(poly: list[Point]) -> list[Msg]:
    '''Returns a list of problems (empty = OK).'''
    problems = []
    n = len(poly)
    if n < 3:
        return [Msg('At least three boundary points are needed.')]
    area2 = sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))
    if abs(area2) < 1e-9:
        problems.append(Msg('The outline has no area (all points on one line?).'))
    for i, j in crossing_edges(poly):
        problems.append(Msg('The boundary crosses itself (edge {a1}-{a2} with edge {b1}-{b2}). '
                            'Please mark the points in order along the boundary.',
                            a1=i + 1, a2=i + 2, b1=j + 1, b2=(j + 1) % n + 1))
    return problems

def clearance(p: Point, poly: list[Point], margin: float,
              holes: 'list[list[Point]] | tuple[()]' = ()) -> float:
    '''
    How far p lies inside the permitted area (outline minus safety margin,
    and also outside all exclusion zones including safety margin).
    >= 0: permitted, < 0: not permitted.
    '''
    d = edge_distance(p, poly)
    c = d - margin if point_in_polygon(p, poly) else -d - margin
    for hole in holes:
        dh = edge_distance(p, hole)
        ch = -dh - margin if point_in_polygon(p, hole) else dh - margin
        if ch < c:
            c = ch
    return c

def exclusion_problems(boundary: list[Point], exclusions: list[list[Point]], home: Point) -> list[Msg]:
    '''Check of the exclusion zones (empty = OK).'''
    problems = []
    for k, zone in enumerate(exclusions, start=1):
        for prob in polygon_problems(zone):
            problems.append(Msg('Exclusion zone {k}: {problem}', k=k, problem=prob))
        if len(zone) >= 3 and point_in_polygon(home, zone):
            problems.append(Msg('Exclusion zone {k} contains the home position.', k=k))
    return problems

def signed_area(poly: list[Point]) -> float:
    '''Signed area (shoelace formula), positive for counterclockwise order.'''
    n = len(poly)
    return 0.5 * sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))

def insert_vertex(poly: list[Point], p: Point, grow: bool = True) -> list[Point] | None:
    '''
    Insert p as a new vertex on the nearest edge that keeps the polygon simple and
    changes its area in the requested direction: grow=True only adds the triangle
    (a, p, b) (expanding the outline), grow=False only cuts it away (shrinking a
    exclusion zone). An edge for which p lies on the wrong side (e.g. in a concave
    pocket of the outline) would otherwise remove area elsewhere. Returns None if
    no edge works.
    '''
    n = len(poly)
    old_area = abs(signed_area(poly))
    order = sorted(range(n), key=lambda i: segment_distance(p, poly[i], poly[(i + 1) % n]))
    for i in order:
        new = poly[:i + 1] + [p] + poly[i + 1:]
        new_area = abs(signed_area(new))
        if (new_area <= old_area) if grow else (new_area >= old_area):
            continue
        # No other vertex may lie in the triangle (a, p, b): otherwise the triangle overlaps the
        # polygon and the result would lose area there even though the total area grows.
        a, b = poly[i], poly[(i + 1) % n]
        tri = [a, p, b]
        if any(point_in_polygon(q, tri) for k, q in enumerate(poly) if k != i and k != (i + 1) % n):
            continue
        if not polygon_problems(new):
            return new
    return None

# ---------------------------------------------------------------------------
# File read/write
# ---------------------------------------------------------------------------

def save_limits(path: str,
                cpr: dict[int, int],
                home_rad: dict[int, float],
                boundary_rad: list[Point],
                exclusions_rad: list[list[Point]] | None = None) -> dict[str, Any]:
    '''Saves home position, outline and exclusion zones.'''
    exclusions_rad = [z for z in (exclusions_rad or []) if z]
    problems = polygon_problems(boundary_rad)
    if problems:
        raise LimitError(join(problems))
    if not point_in_polygon((home_rad[1], home_rad[2]), boundary_rad):
        raise LimitError(Msg('The home position lies outside the outline.'))
    problems = exclusion_problems(boundary_rad, exclusions_rad, (home_rad[1], home_rad[2]))
    if problems:
        raise LimitError(join(problems))
    data: dict[str, Any] = {
        'version': 2,
        'created': datetime.datetime.now().isoformat(timespec='seconds'),
        'cpr': {1: int(cpr[1]), 2: int(cpr[2])},
        'home_deg': {1: deg(home_rad[1]), 2: deg(home_rad[2])},
        'polygon_deg': [[deg(p[0]), deg(p[1])] for p in boundary_rad],
        'exclusions_deg': [[[deg(p[0]), deg(p[1])] for p in zone] for zone in exclusions_rad],
    }
    with open(path, 'w') as f:
        f.write('# NexPlane safe area, created with teach_limits.py\n')
        f.write('# Outline in raw axis angles (degrees, axis 1 / axis 2) relative to the power-on position.\n')
        yaml.safe_dump(data, f, sort_keys=False)
    return data

def load_limits(path: str) -> dict[str, Any]:
    '''Read a limits file written by save_limits(); raises LimitError if it is missing or invalid.'''
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise LimitError(Msg('Limits file not found: {path} (teach it with teach_limits.py first)', path=path))
    if not isinstance(data, dict) or 'polygon_deg' not in data:
        raise LimitError(Msg('Limits file {path} has an old or unknown format. '
                             'Please teach it again with teach_limits.py.', path=path))
    for key in ['cpr', 'home_deg', 'polygon_deg']:
        if key not in data:
            raise LimitError(Msg('Limits file {path} is incomplete: {key} is missing', path=path, key=key))
    return dict(data)

# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

class AxisLimitGuard:
    '''
    Hooks into a SkyWatcher object and limits all motion commands.

    - The combined motion of both axes is predicted. If it approaches
      the boundary of the permitted area, both axes are decelerated
      together and stopped at the boundary.
    - Moves back into the interior remain possible.
    - If an axis moves against the commanded direction, everything is stopped
      and the program is terminated with LimitError.
    '''
    RAY_STEP = rad(0.05)
    MAX_OUTSIDE_RATE = rad(0.5)

    def __init__(self, mount: skywatcher.SkyWatcher, limits: dict[str, Any], settings: dict[str, Any]):
        self.mount = mount
        self.settings = settings

        self.poly: list[Point] = []
        self.holes: list[list[Point]] = []
        self.home: Point = (0.0, 0.0)
        self.profile = str(settings.get('profile') or 'default')
        self._load_geometry(limits)
        self.margin = rad(float(settings['margin_degrees']))
        # Path of the active limits file (for refining and saving from the browser)
        self.file: str | None = resolve_path(settings['file']) if settings.get('file') else None
        self.refine_undo_stack: list[tuple[list[Point], list[list[Point]]]] = []

        self.lookahead = float(settings['lookahead_seconds'])
        if self.lookahead <= 0:
            raise LimitError(Msg('lookahead_seconds must be > 0.'))
        self.trip_dist = rad(float(settings['wrong_way_trip_degrees']))

        self.last_pos: dict[int, float | None] = {1: None, 2: None}
        self.last_time: dict[int, float] = {1: 0.0, 2: 0.0}
        self.wrong_way: dict[int, float] = {1: 0.0, 2: 0.0}
        self.desired: dict[int, float] = {1: 0.0, 2: 0.0}
        self.limited = False
        self.tripped = False
        # Monitoring disabled at own risk (e.g. to point at a target outside the area).
        # The direction check (emergency stop on wrong direction of rotation) stays active.
        self.bypass = False
        # Messages during operation (e.g. can be redirected to a terminal UI).
        self.notify: Callable[[Text], None] = print
        # Measured time between two motion commands per axis (initial value deliberately pessimistic).
        self.cycle: dict[int, float] = {1: 0.5, 2: 0.5}
        self.last_slew_time: dict[int, float | None] = {1: None, 2: None}

        # Hook in (instance attributes override the class methods).
        self._orig_inquire: Callable[[int], float] = mount._inquire_position
        self._orig_slew: Callable[[int, float], None] = mount._slew_axis
        mount._inquire_position = self._inquire_position  # type: ignore[method-assign]
        mount._slew_axis = self._slew_axis  # type: ignore[method-assign]

        if int(limits['cpr'][1]) != mount.cpr[1] or int(limits['cpr'][2]) != mount.cpr[2]:
            raise LimitError(Msg('The steps per revolution do not match the limits file (different motor '
                                 'controller, firmware or simulator?). Please teach the limits again, or '
                                 'comment out axis_limits for simulator tests.'))

        p1 = self._inquire_position(1)
        p2 = self._inquire_position(2)
        if settings['require_home_at_start']:
            tol = rad(float(settings['home_tolerance_degrees']))
            d1 = abs(p1 - rad(limits['home_deg'][1]))
            d2 = abs(p2 - rad(limits['home_deg'][2]))
            if d1 > tol or d2 > tol:
                raise LimitError(Msg(
                    'The mount is not at the taught home position (off by {d1:.2f} degrees on axis 1, '
                    '{d2:.2f} degrees on axis 2). Move the mount to the home position, switch it off and on '
                    'again, then restart.', d1=deg(d1), d2=deg(d2)))
        c = self.clearance_at((p1, p2))
        if c < 0:
            # No abort: only moves back into the permitted area are possible then.
            print(f'WARNING: The mount starts outside the allowed area (including the safety margin, by '
                  f'{deg(-c):.2f} degrees). Only moves back inside are possible.')

        home = (rad(limits['home_deg'][1]), rad(limits['home_deg'][2]))
        print(f'Safe area active (profile {self.profile}): outline with {len(self.poly)} points, '
              f'{len(self.holes)} exclusion zone(s), safety margin {settings["margin_degrees"]} degrees.')
        print(f'  Current position relative to home: axis 1 {deg(p1 - home[0]):+.2f}, '
              f'axis 2 {deg(p2 - home[1]):+.2f} degrees, distance to the edge {deg(c):.2f} degrees.')

    # --- Geometry / profiles ----------------------------------------------

    def _load_geometry(self, limits: dict[str, Any]) -> None:
        '''Take outline, exclusion zones and home position from a limits file (after validating them).'''
        poly = [(rad(a), rad(b)) for a, b in limits['polygon_deg']]
        problems = polygon_problems(poly)
        if problems:
            raise LimitError(Msg('The outline in the limits file is invalid: {problems}', problems=join(problems)))
        holes = [[(rad(a), rad(b)) for a, b in zone] for zone in (limits.get('exclusions_deg') or []) if zone]
        home = (rad(limits['home_deg'][1]), rad(limits['home_deg'][2]))
        problems = exclusion_problems(poly, holes, home)
        if problems:
            raise LimitError(Msg('The exclusion zones in the limits file are invalid: {problems}',
                                 problems=join(problems)))
        self.poly, self.holes, self.home = poly, holes, home

    def clearance_at(self, p: Point) -> float:
        '''Distance from p to the boundary of the permitted area (incl. safety margin), >= 0 permitted.'''
        return clearance(p, self.poly, self.margin, self.holes)

    def set_profile(self, name: str, limits: dict[str, Any]) -> Msg | None:
        '''
        Activate another profile (another setup). Returns a warning if the
        mount is currently outside the new area (then only moves inward
        are possible).
        '''
        if int(limits['cpr'][1]) != self.mount.cpr[1] or int(limits['cpr'][2]) != self.mount.cpr[2]:
            raise LimitError(Msg('Profile {name}: the steps per revolution do not match the mount. '
                                 'Please teach it again.', name=name))
        new_home = (rad(limits['home_deg'][1]), rad(limits['home_deg'][2]))
        tol = rad(float(self.settings['home_tolerance_degrees']))
        if abs(new_home[0] - self.home[0]) > tol or abs(new_home[1] - self.home[1]) > tol:
            raise LimitError(Msg('Profile {name} was taught with a different home position than the active '
                                 'profile. Please teach both profiles at the same home position after '
                                 'switching on.', name=name))
        self._load_geometry(limits)
        self.profile = name
        self.limited = False
        p = (unwrap_pos(self.last_pos[1]), unwrap_pos(self.last_pos[2]))
        c = self.clearance_at(p)
        if c < 0:
            return Msg('The mount is outside the area of profile {name} (by {d:.1f} degrees). '
                       'Only moves back into the allowed area are possible.', name=name, d=deg(-c))
        return None

    # --- Refining the area from the browser -------------------------------

    MIN_REFINE_STEP = rad(0.2)

    def refine_preview(self, p: Point) -> dict[str, Any]:
        '''
        What adding the axis position p as a new boundary vertex would do.
        Returns {'kind': 'outline'|'hole'|'inside'|'invalid', 'poly': [...], 'hole': index, 'msg': Msg}.
        '''
        for k, hole in enumerate(self.holes):
            if point_in_polygon(p, hole):
                new_hole = insert_vertex(hole, p, grow=False)
                if new_hole is None:
                    return {'kind': 'invalid', 'msg': Msg('Exclusion zone {k} cannot be shrunk here.', k=k + 1)}
                holes = [list(h) for h in self.holes]
                holes[k] = new_hole
                err = self._refine_check(self.poly, holes)
                if err:
                    return {'kind': 'invalid', 'msg': err}
                return {'kind': 'hole', 'hole': k, 'poly': new_hole, 'msg': Msg('shrinks exclusion zone {k}', k=k + 1)}
        if point_in_polygon(p, self.poly):
            return {'kind': 'inside', 'msg': Msg('already inside the outline (at most the safety margin blocks here)')}
        if edge_distance(p, self.poly) < self.MIN_REFINE_STEP:
            return {'kind': 'invalid', 'msg': Msg('too close to the current edge (below 0.2 degrees)')}
        new_poly = insert_vertex(self.poly, p, grow=True)
        if new_poly is None:
            return {'kind': 'invalid', 'msg': Msg('no boundary edge where this point only enlarges the outline '
                                                  '(the outline would cross itself or shrink elsewhere)')}
        err = self._refine_check(new_poly, self.holes)
        if err:
            return {'kind': 'invalid', 'msg': err}
        return {'kind': 'outline', 'poly': new_poly, 'msg': Msg('extends the outline')}

    def _refine_check(self, poly: list[Point], holes: list[list[Point]]) -> Msg | None:
        '''Problem with a refined outline and zones (as a message), or None if they are valid.'''
        problems = polygon_problems(poly)
        if problems:
            return problems[0]
        if not point_in_polygon(self.home, poly):
            return Msg('The home position would lie outside the outline.')
        problems = exclusion_problems(poly, holes, self.home)
        return problems[0] if problems else None

    def refine_add(self, p: Point) -> Msg:
        '''
        Add p as a boundary vertex (takes effect immediately, not yet saved). Returns what was
        done, e.g. "extends the outline". Raises LimitError.
        '''
        prev = self.refine_preview(p)
        if prev['kind'] not in ('outline', 'hole'):
            raise LimitError(prev['msg'])
        self.refine_undo_stack.append((list(self.poly), [list(h) for h in self.holes]))
        if prev['kind'] == 'outline':
            self.poly = prev['poly']
        else:
            holes = [list(h) for h in self.holes]
            holes[prev['hole']] = prev['poly']
            self.holes = holes
        self.limited = False
        what: Msg = prev['msg']
        return what

    def refine_undo(self) -> bool:
        if not self.refine_undo_stack:
            return False
        self.poly, self.holes = self.refine_undo_stack.pop()
        self.limited = False
        return True

    def refine_save(self) -> str:
        '''Write the refined area to the active limits file; the previous file is kept as .bak.'''
        if not self.file:
            raise LimitError(Msg('No limits file is known.'))
        if os.path.exists(self.file):
            shutil.copy2(self.file, self.file + '.bak')
        save_limits(self.file, self.mount.cpr, {1: self.home[0], 2: self.home[1]}, self.poly, self.holes)
        self.refine_undo_stack = []
        return self.file

    def refine_discard(self) -> None:
        '''Reload the active limits file (drops unsaved refinements).'''
        if self.file:
            self._load_geometry(load_limits(self.file))
        self.refine_undo_stack = []
        self.limited = False

    # --- Helper functions -------------------------------------------------

    def _emergency_stop(self) -> None:
        '''Stop both axes and refuse all further motion commands.'''
        self.tripped = True
        for axis in [1, 2]:
            try:
                self._orig_slew(axis, 0.0)
            except Exception:
                pass

    def _estimated_position(self, now: float) -> tuple[Point, float]:
        '''Estimated current position (last measurement + distance travelled) and age of the measurement.'''
        ages = []
        est = []
        for axis in [1, 2]:
            if self.last_pos[axis] is None or now - self.last_time[axis] > 0.25:
                self._inquire_position(axis)
            p = self.last_pos[axis]
            assert p is not None
            age = now - self.last_time[axis]
            ages.append(age)
            est.append(p + self.mount.rate[axis] * age)
        return (est[0], est[1]), max(ages)

    def _ray_exit(self, p: Point, ux: float, uy: float, max_dist: float, threshold: float) -> float:
        '''
        Distance along the direction (ux, uy) after which the distance to the boundary
        falls below threshold (inf if that does not happen within max_dist).
        '''
        t = 0.0
        while t <= max_dist:
            t += self.RAY_STEP
            if self.clearance_at((p[0] + ux * t, p[1] + uy * t)) < threshold:
                return t - self.RAY_STEP
        return math.inf

    # --- hooked methods ---------------------------------------------------

    def _inquire_position(self, axis: int) -> float:
        '''
        Replaces SkyWatcher._inquire_position: reads the position and trips the emergency stop if an
        axis moves against the commanded direction.
        '''
        p = self._orig_inquire(axis)
        prev = self.last_pos[axis]
        self.last_pos[axis] = p
        self.last_time[axis] = time.monotonic()

        # Direction check: is the axis moving against the command?
        cmd = self.mount.rate[axis]
        if prev is not None and cmd != 0 and not self.mount.stop_commanded[axis]:
            moved = (p - prev) * math.copysign(1.0, cmd)
            if moved < 0:
                self.wrong_way[axis] += -moved
            elif moved > rad(0.05):
                self.wrong_way[axis] = 0.0
            if self.wrong_way[axis] > self.trip_dist:
                self._emergency_stop()
                raise LimitError(Msg('Axis {axis} moves against the commanded direction. '
                                     'Emergency stop triggered.', axis=axis))
        else:
            self.wrong_way[axis] = 0.0
        return p

    def set_bypass(self, off: bool) -> Msg | None:
        '''Disable/enable monitoring. Returns a warning on enabling, if applicable.'''
        if off == self.bypass:
            return None
        self.bypass = off
        self.limited = False
        self.desired = {1: 0.0, 2: 0.0}
        if off:
            return None
        try:
            p = (self._inquire_position(1), self._inquire_position(2))
        except Exception:
            return None
        c = self.clearance_at(p)
        if c < 0:
            return Msg('The mount is outside the safe area (by {d:.1f} degrees). '
                       'Only moves back into the allowed area are possible.', d=deg(-c))
        return None

    def _slew_axis(self, axis: int, rate: float) -> None:
        '''
        Replaces SkyWatcher._slew_axis: scales the commanded rates of both axes down so that the
        predicted motion stays inside the allowed area (see the class docstring).
        '''
        if self.tripped:
            self._orig_slew(axis, 0.0)
            return
        if self.bypass:
            self.desired[axis] = rate
            self.last_slew_time[axis] = time.monotonic()
            self._orig_slew(axis, rate)
            return

        now = time.monotonic()
        prev_slew = self.last_slew_time[axis]
        if prev_slew is not None:
            dt = min(now - prev_slew, 2.0)
            self.cycle[axis] = dt if dt > self.cycle[axis] else 0.8 * self.cycle[axis] + 0.2 * dt
        self.last_slew_time[axis] = now

        self.desired[axis] = rate
        other = 2 if axis == 1 else 1
        r1, r2 = self.desired[1], self.desired[2]
        speed = math.hypot(r1, r2)
        if speed == 0.0:
            self._orig_slew(axis, 0.0)
            return

        p, age = self._estimated_position(now)
        ux, uy = r1 / speed, r2 / speed
        c_now = self.clearance_at(p)

        # Inside the permitted area, the distance to the boundary must not drop below 0.
        # If the mount is already in the boundary zone (e.g. pushed slightly past the limit),
        # the distance must not decrease further, and the speed is limited.
        in_band = c_now < 0
        threshold = c_now - rad(0.001) if in_band else 0.0
        max_speed = min(speed, self.MAX_OUTSIDE_RATE) if in_band else speed

        # Distance still travelled at the current speed until the next command.
        current_speed = math.hypot(self.mount.rate[1], self.mount.rate[2])
        coast = current_speed * (age + max(self.cycle[1], self.cycle[2]))
        exit_dist = self._ray_exit(p, ux, uy, coast + max_speed * self.lookahead + self.RAY_STEP, threshold)
        allowed = 0.0 if exit_dist <= coast else min(max_speed, (exit_dist - coast) / self.lookahead)

        scale = min(1.0, allowed / speed)
        if scale * speed < rad(0.002):
            scale = 0.0

        is_limited = scale < 1.0
        if is_limited and not self.limited:
            self.notify(Msg('Edge of the safe area reached, motion is limited.'))
        self.limited = is_limited

        new = {1: r1 * scale, 2: r2 * scale}
        self._orig_slew(axis, new[axis])
        # Also decelerate the other axis if it is currently moving too fast.
        cur_other = self.mount.rate[other]
        if abs(new[other]) < abs(cur_other) - 1e-12 or (new[other] == 0.0 and cur_other != 0.0):
            self._orig_slew(other, new[other])

def unwrap_pos(v: float | None) -> float:
    assert v is not None
    return v

def install_from_config(telescope: Any, config_data: dict[str, Any], telescope_protocol: str) -> AxisLimitGuard | None:
    '''Activates the safe area if config.yaml contains an axis_limits section.'''
    settings = get_settings(config_data)
    if settings is None or not settings['file']:
        print('WARNING: No axis limits configured (axis_limits.file in config.yaml).')
        return None
    if not isinstance(telescope, skywatcher.SkyWatcher):
        raise LimitError(Msg('Axis limits are only supported for Sky-Watcher protocols, not {protocol}.',
                             protocol=telescope_protocol))
    limits = load_limits(resolve_path(settings['file']))
    return AxisLimitGuard(telescope, limits, settings)

def profile_path(config_data: dict[str, Any], name: str) -> str:
    settings = get_settings(config_data)
    if settings is None or name not in settings['profiles']:
        raise LimitError(Msg('Unknown profile: {name}', name=name))
    return resolve_path(settings['profiles'][name])

def load_profile(config_data: dict[str, Any], name: str) -> dict[str, Any]:
    settings = get_settings(config_data)
    if settings is None or name not in settings['profiles']:
        raise LimitError(Msg('Unknown profile: {name}', name=name))
    return load_limits(resolve_path(settings['profiles'][name]))
