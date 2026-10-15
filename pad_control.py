'''
Gamepad control for NexPlane (EQ mount, browser UI).

Mapping (PS4 controller):
  Left stick      while tracking: correction (nudge the image); otherwise slew the mount manually.
                  While refining the safe area (aligned): moves the crosshair on the map,
                  stick up = north, right = east, at the selected flight altitude
  Right stick     fine correction or slow slewing
  R2 (analog)     faster (search)
  D-pad           small correction steps
  Cross           alignment point: aircraft is centered -> update model (polar error from 3 points)
  Circle          reset correction to 0
  Square          delete last alignment point (if centered wrongly)
  Triangle        STOP (tracking and slewing)
  R1 / L1         next / previous aircraft (before alignment: aiming target)
  Options         cycle focal length for alignment points (vibrates 1/2/3 times)
  Share           while refining the safe area: accept the current axis position as a new limit point

The correction is applied in the camera image frame: "stick up" moves the mount so that
the image always moves the same way, regardless of where the telescope points. If the
direction does not match the camera, set invert_x / invert_y / swap_xy under gamepad in config.yaml.
'''

import math
import time

from typing import Any

DEFAULTS: dict[str, Any] = {
    'enabled': True,
    'name': '',                       # part of the device name, empty = first gamepad
    'device': None,                   # or the device file directly, e.g. /dev/input/event17
    'invert_x': False,
    'invert_y': False,
    'swap_xy': False,
    'deadzone': 0.12,
    'correction_deg_per_s': 0.4,      # correction at full deflection
    'fast_factor': 6.0,               # with R2 fully pressed
    'fine_factor': 0.15,              # right stick
    'step_degrees': 0.02,             # D-pad
    'jog_deg_per_s': 1.0,             # slewing without a target at full deflection
    'jog_fast_deg_per_s': 5.0,        # with R2 fully pressed
    'min_elevation_degrees': 8.0,     # R1/L1 only select aircraft above this
    'rumble': True,
}

def get_settings(config_data: dict[str, Any]) -> dict[str, Any]:
    s = dict(DEFAULTS)
    s.update(config_data.get('gamepad') or {})
    return s

class PadControl:
    '''Turns gamepad sticks and buttons into correction and slew rates (see the module docstring).'''
    def __init__(self, settings: dict[str, Any], pad: Any):
        self.s = settings
        self.pad = pad

    def _image_vec(self, x: float, y: float) -> tuple[float, float]:
        '''Apply swap_xy, invert_x and invert_y from the config (camera orientation).'''
        if self.s['swap_xy']:
            x, y = y, x
        if self.s['invert_x']:
            x = -x
        if self.s['invert_y']:
            y = -y
        return x, y

    @staticmethod
    def _expo(v: float) -> float:
        # sensitive around the center, full deflection stays 1
        return v * abs(v)

    def stick(self, axes: dict[str, float]) -> tuple[float, float, float, float]:
        '''Returns (x, y) for the left stick and (fx, fy) for the right stick in the image frame, -1..1.'''
        x, y = self._image_vec(self._expo(axes['lx']), self._expo(axes['ly']))
        fx, fy = self._image_vec(self._expo(axes['rx']), self._expo(axes['ry']))
        return x, y, fx, fy

    def correction_rate(self, axes: dict[str, float]) -> tuple[float, float]:
        '''Correction rate in the image frame (rad/s).'''
        x, y, fx, fy = self.stick(axes)
        base = math.radians(float(self.s['correction_deg_per_s']))
        fast = 1.0 + (float(self.s['fast_factor']) - 1.0) * axes['r2']
        fine = float(self.s['fine_factor'])
        return (x * fast + fx * fine) * base, (y * fast + fy * fine) * base

    def jog_rate(self, axes: dict[str, float]) -> tuple[float, float]:
        '''Slew rate in the image frame (rad/s) when no target is being tracked.'''
        x, y, fx, fy = self.stick(axes)
        slow = float(self.s['jog_deg_per_s'])
        fast = float(self.s['jog_fast_deg_per_s'])
        rate = math.radians(slow + (fast - slow) * axes['r2'])
        fine = float(self.s['fine_factor'])
        return (x + fx * fine) * rate, (y + fy * fine) * rate

    def map_stick(self, axes: dict[str, float]) -> tuple[float, float, float]:
        '''
        Stick in the map frame for refining the limits: (east, north) unit direction and the
        angular speed (rad/s). No camera inversion: stick up = north on the map, right = east.
        '''
        e = self._expo(axes['lx']) + self._expo(axes['rx']) * float(self.s['fine_factor'])
        n = self._expo(axes['ly']) + self._expo(axes['ry']) * float(self.s['fine_factor'])
        mag = math.hypot(e, n)
        if mag < 1e-9:
            return 0.0, 0.0, 0.0
        slow = float(self.s['jog_deg_per_s'])
        fast = float(self.s['jog_fast_deg_per_s'])
        rate = math.radians(slow + (fast - slow) * axes['r2']) * min(1.0, mag)
        return e / mag, n / mag, rate

    def step(self, press: str) -> tuple[float, float] | None:
        '''Correction step for a D-pad press in the image frame (rad), or None for other buttons.'''
        st = math.radians(float(self.s['step_degrees']))
        vec = {'up': (0, 1), 'down': (0, -1), 'left': (-1, 0), 'right': (1, 0)}.get(press)
        if vec is None:
            return None
        x, y = self._image_vec(float(vec[0]), float(vec[1]))
        return x * st, y * st

# ---------------------------------------------------------------------------
# Map-frame control (crosshair on the ground map at a flight altitude)
# ---------------------------------------------------------------------------

EARTH_R = 6371000.0
MAX_MAP_DIST = 450000.0   # same range as the map in the browser

def _el_at(d: float, dh: float) -> float:
    '''Elevation under which a point at height dh above the observer appears at ground distance d.'''
    return math.atan2(dh - d * d / (2 * EARTH_R), d)

def map_distance(el: float, dh: float) -> float | None:
    '''Ground distance at which the line of sight with elevation el reaches height dh (None: not on the map).'''
    if dh <= 0 or el <= 0 or _el_at(MAX_MAP_DIST, dh) >= el:
        return None
    lo, hi = 1.0, MAX_MAP_DIST
    for _ in range(50):
        mid = (lo + hi) / 2
        if _el_at(mid, dh) > el:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2

def map_to_altaz_rate(east: float, north: float, speed: float, az: float, el: float,
                      dh: float) -> tuple[float, float]:
    '''
    Direction on the ground map (unit vector east/north) -> rates (d_az, d_el) in rad/s so that the
    crosshair at height dh moves in exactly that map direction. The angular speed on the sky is
    'speed', so the mount moves at the usual jog speed regardless of the distance.
    Azimuth from north over east.
    '''
    v_r = east * math.sin(az) + north * math.cos(az)     # away from the observer
    v_t = east * math.cos(az) - north * math.sin(az)     # clockwise (increasing azimuth)
    d = map_distance(el, dh)
    if d is not None:
        h = max(1.0, d * 1e-4)
        del_dd = (_el_at(d + h, dh) - _el_at(d - h, dh)) / (2 * h)    # negative: farther = lower
        d_az = v_t / d
        d_el = v_r * del_dd
    else:
        # Line of sight does not reach the altitude on the map: away = lower, sideways = azimuth
        d_az = v_t / max(math.cos(el), 0.05)
        d_el = -v_r
    sky = math.hypot(d_az * math.cos(el), d_el)
    if sky < 1e-12:
        return 0.0, 0.0
    return d_az * speed / sky, d_el * speed / sky

def altaz_to_hadec(az: float, el: float, lat: float) -> tuple[float, float]:
    '''Azimuth (from north over east) / elevation -> hour angle / declination.'''
    sd = math.sin(el) * math.sin(lat) + math.cos(el) * math.cos(lat) * math.cos(az)
    dec = math.asin(max(-1.0, min(1.0, sd)))
    ha = math.atan2(-math.sin(az) * math.cos(el),
                    math.sin(el) * math.cos(lat) - math.cos(el) * math.cos(az) * math.sin(lat))
    return ha, dec

def _wrap_pi(x: float) -> float:
    return (x + math.pi) % (2 * math.pi) - math.pi

def altaz_rate_to_radec_rate(az: float, el: float, d_az: float, d_el: float,
                             lat: float) -> tuple[float, float]:
    '''Rates in az/el -> rates in RA/Dec (instantaneous, RA = LST - HA).'''
    m = math.hypot(d_az, d_el)
    if m < 1e-12:
        return 0.0, 0.0
    k = 1e-4 / m
    ha0, dec0 = altaz_to_hadec(az, el, lat)
    ha1, dec1 = altaz_to_hadec(az + d_az * k, el + d_el * k, lat)
    return -_wrap_pi(ha1 - ha0) / k, (dec1 - dec0) / k

def radec_rate_to_raw_rate(raw_to_radec: Any, ra_raw: float, dec_raw: float,
                           d_ra: float, d_dec: float) -> tuple[float, float] | None:
    '''
    RA/Dec rates -> axis rates (ra_raw, dec_raw), via the numeric Jacobian of the pointing
    function raw_to_radec (covers both pier sides and the polar-error model).
    '''
    h = 1e-4
    r0 = raw_to_radec(ra_raw - h, dec_raw)
    r1 = raw_to_radec(ra_raw + h, dec_raw)
    s0 = raw_to_radec(ra_raw, dec_raw - h)
    s1 = raw_to_radec(ra_raw, dec_raw + h)
    a = _wrap_pi(r1[0] - r0[0]) / (2 * h)
    c = (r1[1] - r0[1]) / (2 * h)
    b = _wrap_pi(s1[0] - s0[0]) / (2 * h)
    d = (s1[1] - s0[1]) / (2 * h)
    det = a * d - b * c
    if abs(det) < 1e-9:
        return None
    # Near the pole an RA rate needs a huge axis rate: limit like image_to_axes (cos >= 0.15)
    if abs(det) < 0.15:
        det = math.copysign(0.15, det)
    return (d * d_ra - b * d_dec) / det, (-c * d_ra + a * d_dec) / det

# ---------------------------------------------------------------------------
# Aircraft selection with R1/L1
# ---------------------------------------------------------------------------

def _ang_dist(az1: float, el1: float, az2: float, el2: float) -> float:
    c = math.sin(el1) * math.sin(el2) + math.cos(el1) * math.cos(el2) * math.cos(az1 - az2)
    return math.acos(max(-1.0, min(1.0, c)))

def pick_next(planes: dict[str, Any], scope_az: float, scope_el: float, current: str | None,
              recent: list[str], min_el: float, usable: Any = None) -> str | None:
    '''
    Aircraft closest to the current pointing direction (excluding the current one and the
    recently visited ones). usable(hex) can exclude aircraft (e.g. not reachable).
    '''
    now_ns = time.monotonic_ns()
    cands = []
    for hex_id, a in planes.items():
        if hex_id == current:
            continue
        try:
            ex = a.extrapolate(now_ns)
            az = float(ex.az.value)
            el = float(ex.el.value)
            if a.lat_time_ns is None or (now_ns - a.lat_time_ns) / 1e9 > 15 or bool(a.in_space.value):
                continue
        except Exception:
            continue
        if el < min_el:
            continue
        cands.append((_ang_dist(scope_az, scope_el, az, el), hex_id))
    cands.sort()
    for only_new in (True, False):
        for _, hex_id in cands:
            if only_new and hex_id in recent:
                continue
            if usable is not None and not usable(hex_id):
                continue
            return hex_id
    return None

def pick_aim(planes: dict[str, Any], current: str | None, recent: list[str], min_el: float) -> str | None:
    '''
    Aiming target before alignment: the telescope's pointing direction is still unknown, so pick
    the aircraft that moves slowest across the sky (easiest to find manually),
    at least 15 degrees high.
    '''
    now_ns = time.monotonic_ns()
    cands = []
    for hex_id, a in planes.items():
        if hex_id == current:
            continue
        try:
            if a.lat_time_ns is None or (now_ns - a.lat_time_ns) / 1e9 > 15 or bool(a.in_space.value):
                continue
            ex0 = a.extrapolate(now_ns)
            ex1 = a.extrapolate(now_ns + 1_000_000_000)
            el = float(ex0.el.value)
            rate = _ang_dist(float(ex0.az.value), el, float(ex1.az.value), float(ex1.el.value))
        except Exception:
            continue
        if el < max(min_el, math.radians(15.0)):
            continue
        cands.append((rate, hex_id))
    cands.sort()
    for only_new in (True, False):
        for _, hex_id in cands:
            if only_new and hex_id in recent:
                continue
            return hex_id
    return None
