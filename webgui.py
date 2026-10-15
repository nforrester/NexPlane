'''
Browser GUI for NexPlane.

Replaces the OpenGL GUI (gui.py) when nexplane.py is started with
--web. A small HTTP server runs on this machine only (127.0.0.1) and
serves the page webgui.html plus the current state as JSON. The page
shows either a sky map or a ground map; clicking on a
plane starts tracking it.

The WebGui class provides the same methods that nexplane.py uses on the
OpenGL layers (update_planes, get_tracked_plane, stop_tracking,
get_gains, get_offsets, reset_offsets, update_positions,
update_telescope_location, update_comm_failure), so that the main loop
stays unchanged.

Texts for the user are sent as messages.Msg (English template plus parameters); the page
translates them into the language selected in the browser (see webgui_static/i18n_de.js).
'''

import collections
import json
import math
import os
import secrets
import threading
import time
import webbrowser

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import astropy.coordinates as coords
import astropy.units as units
import numpy

import util

from messages import Msg, Text, to_json
from sbs1 import Airplane
from util import unwrap

HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webgui.html')
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webgui_static')
CONTENT_TYPES = {'.js': 'application/javascript', '.css': 'text/css', '.png': 'image/png',
                 '.svg': 'image/svg+xml', '.txt': 'text/plain'}

class Exit(Exception):
    '''Raised in the main loop when NexPlane should exit.'''
    pass

def _deg(r: float) -> float:
    return r * 180.0 / math.pi

def _error(msg: Msg) -> dict[str, Any]:
    '''Reply to a GUI command that was refused.'''
    return {'ok': False, 'error': msg}

def _json_default(obj: Any) -> Any:
    '''json.dumps() helper: messages are sent as template plus parameters.'''
    if isinstance(obj, Msg):
        return obj.to_json()
    raise TypeError(f'{type(obj).__name__} is not JSON serializable')

class WebGui:
    '''
    Browser GUI: a local HTTP server plus the interface the main loop expects from the GUI layers.
    The HTTP handler threads and the main loop share the state below; every access holds self.lock.
    '''
    def __init__(self, observatory_location: coords.EarthLocation, kp: float, ki: float, kd: float,
                 port: int = 8765, open_browser: bool = True, mount_mode: str = 'eq'):
        self.observatory = observatory_location
        self.mount_mode = mount_mode
        self.lock = threading.Lock()

        # Provided by the main program
        self.planes: dict[str, Airplane] = {}
        self.telescope_azm_alt: tuple[float, float] | None = None
        self.sun_azm_alt: tuple[float, float] | None = None
        self.moon_azm_alt: tuple[float, float] | None = None
        self.comm_failure = False
        self.messages: collections.deque[tuple[float, Any]] = collections.deque(maxlen=20)   # (time, to_json(text))

        # Set by the GUI
        self.tracked_plane: str | None = None
        self.fixed_target: dict[str, Any] | None = None   # {'az','el' (rad), 'side'} for the pier-side flip test
        self.scope_side: str | None = None
        self.target_side: str | None = None
        self.auto_flip = False
        self.azm_offset = 0.0
        self.alt_offset = 0.0
        self.kp, self.ki, self.kd = kp, ki, kd
        self.gain_changes = 0
        self.quit_requested = False

        # Manual control, homing and alignment (only if nexplane.py offers them)
        self.manual_available = False
        self.jog_rates = (0.0, 0.0)          # deg/s per axis
        self.jog_time = 0.0                  # last jog command (dead man's switch)
        self.home_requested = False
        self.home_source: Text = ''
        self.align_request: str | None = None
        self.align_targets: list[str] = []
        self.aligned = False
        self.aligned_text: Text = ''
        self.location = ''
        self.location_presets: dict[str, tuple[float, float, float]] = {}
        self.location_request: str | None = None
        self.profiles: list[str] = []
        self.profile = ''
        self.profile_request: str | None = None
        self.limits_off = False
        self.axis_rel: list[float] | None = None   # axis position relative to the taught home (degrees)
        self.reach: dict[str, Any] | None = None
        self.reach_status: Text = ''
        self.reach_version = 0

        # Observer's geocentric position and directions (for plane latitude/longitude)
        self.obs_xyz = numpy.array([c.to(units.m).value for c in observatory_location.to_geocentric()])
        self.n_unit, self.e_unit, self.d_unit = util.ned_unit_vectors_at_earth_location(observatory_location)
        lon, lat, height = observatory_location.to_geodetic('WGS84')
        self.obs_geodetic = (float(lat.to(units.deg).value), float(lon.to(units.deg).value),
                             float(height.to(units.m).value))

        # Recompute the celestial pole only every few seconds (slow)
        self.pole_cache: tuple[float, tuple[float, float]] | None = None

        # Protection against requests from foreign websites: every API request needs this token in the header.
        self.token = secrets.token_urlsafe(24)

        handler = self._make_handler()
        self.server = ThreadingHTTPServer(('127.0.0.1', port), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{port}/'
        print(f'Browser GUI: {self.url}')
        if open_browser:
            try:
                webbrowser.open(self.url)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Interface for the main loop (same as gui.py)
    # ------------------------------------------------------------------

    def exit_if_dead(self) -> None:
        if self.quit_requested or not self.thread.is_alive():
            raise Exit

    def close(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass

    def update_planes(self, airplanes: dict[str, Airplane]) -> None:
        self.exit_if_dead()
        with self.lock:
            self.planes = airplanes   # get_planes() already returns a copy

    def get_tracked_plane(self) -> str | None:
        self.exit_if_dead()
        with self.lock:
            return self.tracked_plane

    def stop_tracking(self) -> None:
        self.exit_if_dead()
        with self.lock:
            self.tracked_plane = None
            self.fixed_target = None

    def get_fixed_target(self) -> dict[str, Any] | None:
        self.exit_if_dead()
        with self.lock:
            return dict(self.fixed_target) if self.fixed_target is not None else None

    def set_fixed_side(self, side: str) -> None:
        with self.lock:
            if self.fixed_target is not None:
                self.fixed_target['side'] = side

    def update_side(self, scope_side: str, target_side: str | None, auto_flip: bool) -> None:
        with self.lock:
            self.scope_side = scope_side
            self.target_side = target_side
            self.auto_flip = auto_flip

    def get_gains(self) -> tuple[float, float, float, int]:
        self.exit_if_dead()
        with self.lock:
            return self.kp, self.ki, self.kd, self.gain_changes

    def get_offsets(self) -> tuple[float, float]:
        self.exit_if_dead()
        with self.lock:
            return self.azm_offset, self.alt_offset

    def reset_offsets(self) -> None:
        self.exit_if_dead()
        with self.lock:
            self.azm_offset = 0.0
            self.alt_offset = 0.0

    def update_positions(self, sun_azm_alt: tuple[float, float], moon_azm_alt: tuple[float, float]) -> None:
        self.exit_if_dead()
        with self.lock:
            self.sun_azm_alt = sun_azm_alt
            self.moon_azm_alt = moon_azm_alt

    def update_telescope_location(self, scope_azm_alt: tuple[float, float]) -> None:
        self.exit_if_dead()
        with self.lock:
            self.telescope_azm_alt = scope_azm_alt

    def update_comm_failure(self, warn_comm_failure: bool) -> None:
        self.exit_if_dead()
        with self.lock:
            self.comm_failure = warn_comm_failure

    # --- Manual control / home / alignment (main loop) ---

    JOG_DEADMAN_S = 0.5

    def enable_manual(self, home_source: Text, align_targets: list[str], aligned: bool, aligned_text: Text) -> None:
        with self.lock:
            self.manual_available = True
            self.home_source = home_source
            self.align_targets = list(align_targets)
            self.aligned = aligned
            self.aligned_text = aligned_text

    def set_locations(self, current: str, presets: dict[str, tuple[float, float, float]]) -> None:
        with self.lock:
            self.location = current
            self.location_presets = dict(presets)

    def set_profiles(self, names: list[str], active: str) -> None:
        with self.lock:
            self.profiles = list(names)
            self.profile = active

    def pop_profile_request(self) -> str | None:
        with self.lock:
            req, self.profile_request = self.profile_request, None
            return req

    def set_axis_rel(self, a1: float, a2: float) -> None:
        with self.lock:
            self.axis_rel = [round(a1, 2), round(a2, 2)]

    def get_limits_off(self) -> bool:
        with self.lock:
            return self.limits_off and bool(self.profiles)

    def is_aligned(self) -> bool:
        with self.lock:
            return self.aligned

    def set_reach(self, grid: dict[str, Any] | None, error: Text = '') -> None:
        '''Grid of reachable sky regions (from reach_map), or an error message if the calculation failed.'''
        with self.lock:
            if grid is not None:
                self.reach = grid
                self.reach_status = ''
            else:
                self.reach_status = error
                if error:
                    self.messages.append((time.time(), to_json(error)))
            self.reach_version += 1

    def set_reach_calculating(self) -> None:
        '''The grid of reachable sky regions is being recalculated.'''
        with self.lock:
            self.reach_status = Msg('calculating …')
            self.reach_version += 1

    def pop_location_request(self) -> str | None:
        self.exit_if_dead()
        with self.lock:
            req, self.location_request = self.location_request, None
            return req

    def get_jog(self) -> tuple[float, float] | None:
        '''Current manual control rates in rad/s, or None (no button pressed / dead man's switch expired).'''
        self.exit_if_dead()
        with self.lock:
            if time.monotonic() - self.jog_time > self.JOG_DEADMAN_S:
                return None
            if self.jog_rates == (0.0, 0.0):
                return None
            return (self.jog_rates[0] / 180 * math.pi, self.jog_rates[1] / 180 * math.pi)

    def home_active(self) -> bool:
        with self.lock:
            return self.home_requested

    def home_done(self) -> None:
        with self.lock:
            self.home_requested = False

    def pop_align_request(self) -> str | None:
        with self.lock:
            req, self.align_request = self.align_request, None
            return req

    def set_aligned(self, text: Text) -> None:
        with self.lock:
            self.aligned = True
            self.aligned_text = text

    def notify(self, text: Text) -> None:
        '''Show a message (e.g. from the limit monitor) in the GUI and, in English, in the terminal.'''
        with self.lock:
            self.messages.append((time.time(), to_json(text)))
        print(text)

    # ------------------------------------------------------------------
    # State for the GUI
    # ------------------------------------------------------------------

    def _pole(self) -> tuple[float, float] | None:
        if self.mount_mode != 'eq':
            return None
        now = time.monotonic()
        if self.pole_cache is None or now - self.pole_cache[0] > 10:
            alt, azm = util.radec_to_altaz(0.0, math.pi / 2, self.observatory, util.get_current_time())
            self.pole_cache = (now, (_deg(azm), _deg(alt)))
        return self.pole_cache[1]

    def state(self) -> dict[str, Any]:
        '''Everything the page shows, as JSON-ready data (polled by the page several times per second).'''
        with self.lock:
            planes = dict(self.planes)
            tracked = self.tracked_plane
            telescope = self.telescope_azm_alt
            sun, moon = self.sun_azm_alt, self.moon_azm_alt
            out: dict[str, Any] = {
                'offsets': {'az': _deg(self.azm_offset), 'el': _deg(self.alt_offset)},
                'gains': {'kp': self.kp, 'ki': self.ki, 'kd': self.kd},
                'comm_failure': self.comm_failure,
                'messages': [{'t': t, 'text': m} for t, m in self.messages],
                'scope_side': self.scope_side,
                'target_side': self.target_side,
                'auto_flip': self.auto_flip,
                'profiles': {'list': self.profiles, 'active': self.profile},
                'limits_off': self.limits_off and bool(self.profiles),
                'axis_rel': self.axis_rel,
                'reach': {'version': self.reach_version, 'available': self.reach is not None,
                          'status': to_json(self.reach_status)},
                'location': {'current': self.location,
                             'presets': [{'name': n, 'lat': v[0], 'lon': v[1], 'alt': v[2]}
                                         for n, v in self.location_presets.items()]},
                'manual': {'available': self.manual_available, 'home_source': to_json(self.home_source),
                           'home_active': self.home_requested, 'align_targets': self.align_targets,
                           'aligned': self.aligned, 'aligned_text': to_json(self.aligned_text),
                           'jogging': time.monotonic() - self.jog_time <= self.JOG_DEADMAN_S and self.jog_rates != (0.0, 0.0)},
                'fixed_target': ({'az': _deg(self.fixed_target['az']), 'el': _deg(self.fixed_target['el']),
                                  'side': self.fixed_target['side']} if self.fixed_target else None),
            }

        now_ns = time.monotonic_ns()
        plane_list = []
        ned_rows = []
        for hex_id, a in planes.items():
            try:
                pos = numpy.asarray(unwrap(a.pos_ned.value), dtype=float)
                vel = numpy.asarray(unwrap(a.vel_ned.value), dtype=float)
                az = _deg(unwrap(a.az.value))
                el = _deg(unwrap(a.el.value))
                rng = float(unwrap(a.range.value))
            except Exception:
                continue
            r2 = float(numpy.dot(pos, pos))
            ang_rate = _deg(float(numpy.linalg.norm(numpy.cross(pos, vel))) / r2) if r2 > 0 else 0.0
            age = (now_ns - a.lat_time_ns) / 1e9 if a.lat_time_ns is not None else None
            plane_list.append({
                'hex': hex_id,
                'callsign': (a.callsign.value or '').strip(),
                'az': az, 'el': el, 'range_m': rng,
                'speed_ms': float(math.hypot(vel[0], vel[1])),
                'track_deg': (_deg(math.atan2(vel[1], vel[0])) + 360.0) % 360.0,
                'vrate_ms': float(-vel[2]),
                'ang_rate_dps': ang_rate,
                'in_space': bool(a.in_space.value),
                'age_s': age,
                'tracked': hex_id == tracked,
            })
            ned_rows.append(pos)

        # Latitude, longitude and altitude of all planes in one computation step
        if ned_rows:
            ned = numpy.array(ned_rows)
            xyz = self.obs_xyz + ned[:, 0:1] * self.n_unit + ned[:, 1:2] * self.e_unit + ned[:, 2:3] * self.d_unit
            loc = coords.EarthLocation.from_geocentric(xyz[:, 0], xyz[:, 1], xyz[:, 2], unit=units.m)
            lon, lat, h = loc.to_geodetic('WGS84')
            lat_d = numpy.atleast_1d(lat.to(units.deg).value)
            lon_d = numpy.atleast_1d(lon.to(units.deg).value)
            h_m = numpy.atleast_1d(h.to(units.m).value)
            for i, p in enumerate(plane_list):
                p['lat'] = float(lat_d[i])
                p['lon'] = float(lon_d[i])
                p['alt_m'] = float(h_m[i])

        tracked_now = None
        if tracked is not None and tracked in planes:
            try:
                ex = planes[tracked].extrapolate(now_ns)
                tracked_now = {'az': _deg(unwrap(ex.az.value)), 'el': _deg(unwrap(ex.el.value))}
            except Exception:
                tracked_now = None

        out.update({
            'server_time': time.time(),
            'observer': {'lat': self.obs_geodetic[0], 'lon': self.obs_geodetic[1], 'alt_m': self.obs_geodetic[2]},
            'mount_mode': self.mount_mode,
            'telescope': {'az': _deg(telescope[0]), 'el': _deg(telescope[1])} if telescope else None,
            'sun': {'az': _deg(sun[0]), 'el': _deg(sun[1])} if sun else None,
            'moon': {'az': _deg(moon[0]), 'el': _deg(moon[1])} if moon else None,
            'pole': None,
            'tracked': tracked,
            'tracked_now': tracked_now,
            'planes': plane_list,
        })
        pole = self._pole()
        if pole is not None:
            out['pole'] = {'az': pole[0], 'el': pole[1]}
        return out

    # ------------------------------------------------------------------
    # Commands from the GUI
    # ------------------------------------------------------------------

    def command(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        '''
        Handle a command from the page (POST /api/<name>). Commands that need the mount are queued for
        the main loop; returns {ok: True} or {ok: False, error: message}.
        '''
        with self.lock:
            if name in ('track', 'flip_test') and self.manual_available and not self.aligned:
                return _error(Msg('Align first (card "Control & alignment").'))
            if name in ('track', 'flip_test') and self.limits_off:
                return _error(Msg('The safe area is disabled. Re-enable it first.'))
            if name == 'limits_off':
                if not self.profiles:
                    return _error(Msg('No safe area configured'))
                off = bool(data.get('off'))
                if off and data.get('confirm') is not True:
                    return _error(Msg('Confirmation missing'))
                if off:
                    self.tracked_plane = None
                    self.fixed_target = None
                self.limits_off = off
                return {'ok': True}
            if name == 'set_profile':
                prof = data.get('name')
                if not isinstance(prof, str) or prof not in self.profiles:
                    return _error(Msg('Unknown profile'))
                if prof == self.profile:
                    return _error(Msg('This profile is already active'))
                self.tracked_plane = None
                self.fixed_target = None
                self.home_requested = False
                self.jog_rates = (0.0, 0.0)
                self.profile_request = prof
                return {'ok': True}
            if name == 'set_location':
                loc = data.get('name')
                if not isinstance(loc, str) or loc not in self.location_presets:
                    return _error(Msg('Unknown location'))
                if loc == self.location:
                    return _error(Msg('This location is already active'))
                self.tracked_plane = None
                self.fixed_target = None
                self.home_requested = False
                self.jog_rates = (0.0, 0.0)
                self.location_request = loc
                return {'ok': True}
            if name == 'jog':
                if not self.manual_available:
                    return _error(Msg('Manual control not available'))
                r1 = float(data.get('r1', 0.0))
                r2 = float(data.get('r2', 0.0))
                if abs(r1) > 5.0 or abs(r2) > 5.0:
                    return _error(Msg('Speed too high'))
                self.tracked_plane = None
                self.fixed_target = None
                self.home_requested = False
                self.jog_rates = (r1, r2)
                self.jog_time = time.monotonic()
                return {'ok': True}
            if name == 'jog_stop':
                self.jog_rates = (0.0, 0.0)
                return {'ok': True}
            if name == 'home':
                if not self.manual_available:
                    return _error(Msg('Go-home not available'))
                self.tracked_plane = None
                self.fixed_target = None
                self.jog_rates = (0.0, 0.0)
                self.home_requested = True
                return {'ok': True}
            if name == 'align':
                target = data.get('target')
                if not self.manual_available or not isinstance(target, str) or not target.strip():
                    return _error(Msg('No alignment target given'))
                target = target.strip()
                if not target.startswith('sky:') and target not in self.align_targets:
                    return _error(Msg('Unknown landmark'))
                self.tracked_plane = None
                self.fixed_target = None
                self.home_requested = False
                self.jog_rates = (0.0, 0.0)
                self.align_request = target
                return {'ok': True}
            if name == 'track':
                hex_id = data.get('hex')
                if not isinstance(hex_id, str) or hex_id not in self.planes:
                    return _error(Msg('Unknown target'))
                self.tracked_plane = hex_id
                self.fixed_target = None
                self.azm_offset = 0.0
                self.alt_offset = 0.0
            elif name == 'stop':
                self.tracked_plane = None
                self.fixed_target = None
                self.home_requested = False
                self.jog_rates = (0.0, 0.0)
            elif name == 'flip_test':
                if self.telescope_azm_alt is None or self.mount_mode != 'eq':
                    return _error(Msg('Side change only in EQ mode with a known telescope position'))
                az, el = self.telescope_azm_alt
                if el < 5 / 180 * math.pi:
                    return _error(Msg('The telescope points too low (below 5 degrees) for the test'))
                self.tracked_plane = None
                self.fixed_target = {'az': az, 'el': el, 'side': None}
                self.azm_offset = 0.0
                self.alt_offset = 0.0
            elif name == 'offset':
                daz = float(data.get('daz', 0.0))
                dele = float(data.get('del', 0.0))
                if abs(daz) > 5 or abs(dele) > 5:
                    return _error(Msg('Correction too large'))
                self.azm_offset += daz / 180 * math.pi
                self.alt_offset += dele / 180 * math.pi
            elif name == 'offset_reset':
                self.azm_offset = 0.0
                self.alt_offset = 0.0
            elif name == 'gain':
                which = data.get('which')
                delta = float(data.get('delta', 0.0))
                if which not in ('kp', 'ki', 'kd') or abs(delta) > 0.1:
                    return _error(Msg('Invalid controller value'))
                setattr(self, which, getattr(self, which) + delta)
                self.gain_changes += 1
            elif name == 'quit':
                self.quit_requested = True
            else:
                return _error(Msg('Unknown command'))
        return {'ok': True}

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        '''
        HTTP request handler bound to this WebGui. Only requests to 127.0.0.1/localhost are served,
        and every API call needs the token of this run.
        '''
        webgui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj, default=_json_default).encode('utf-8'), 'application/json; charset=utf-8')

            def _host_ok(self) -> bool:
                host = (self.headers.get('Host') or '').split(':')[0]
                return host in ('127.0.0.1', 'localhost')

            def _token_ok(self) -> bool:
                return secrets.compare_digest(self.headers.get('X-NexPlane-Token', ''), webgui.token)

            def do_GET(self) -> None:
                if not self._host_ok():
                    self._send(403, b'forbidden', 'text/plain')
                    return
                if self.path in ('/', '/index.html'):
                    try:
                        with open(HTML_FILE, encoding='utf-8') as f:
                            html = f.read().replace('__NEXPLANE_TOKEN__', webgui.token)
                    except OSError:
                        self._send(500, b'webgui.html is missing', 'text/plain; charset=utf-8')
                        return
                    self._send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
                elif self.path.startswith('/static/'):
                    rel = os.path.normpath(self.path[len('/static/'):].split('?')[0])
                    full = os.path.join(STATIC_DIR, rel)
                    ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower())
                    if rel.startswith('..') or os.path.isabs(rel) or ctype is None or not os.path.isfile(full):
                        self._send(404, b'not found', 'text/plain')
                        return
                    with open(full, 'rb') as f:
                        self._send(200, f.read(), ctype)
                elif self.path == '/api/reach':
                    if not self._token_ok():
                        self._send(403, b'forbidden', 'text/plain')
                        return
                    with webgui.lock:
                        grid = webgui.reach
                        version = webgui.reach_version
                    self._json(200, {'version': version, 'grid': grid})
                elif self.path == '/api/state':
                    if not self._token_ok():
                        self._send(403, b'forbidden', 'text/plain')
                        return
                    try:
                        self._json(200, webgui.state())
                    except Exception as e:
                        self._json(500, {'error': str(e)})
                else:
                    self._send(404, b'not found', 'text/plain')

            def do_POST(self) -> None:
                if not self._host_ok() or not self._token_ok() or not self.path.startswith('/api/'):
                    self._send(403, b'forbidden', 'text/plain')
                    return
                length = int(self.headers.get('Content-Length') or 0)
                if length > 10000:
                    self._send(413, b'too large', 'text/plain')
                    return
                try:
                    data = json.loads(self.rfile.read(length) or b'{}')
                    if not isinstance(data, dict):
                        raise ValueError
                except Exception:
                    self._json(400, _error(Msg('Invalid request')))
                    return
                self._json(200, webgui.command(self.path[len('/api/'):], data))

        return Handler
