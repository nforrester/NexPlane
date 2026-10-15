#!/usr/bin/env python3

'''The main application for tracking airplanes and satellites with a NexStar telescope.'''

import argparse
import math
import os
import sys
import time
import traceback
import yaml

import astropy.units as units

from typing import Any

import align
import axis_limits
import eq_flip
import gamepad
import gps
import pad_control
import places
import reach_map
import astronomical
import config
import nexstar
import rpc
import skywatcher
import sbs1
import tracker
import tracking_log
import util

from messages import Msg, Text, of_exception, to_json
from util import unwrap

# The OpenGL GUI is only needed without --web. If OpenGL/GLUT is missing,
# NexPlane still runs with --web.
gui: Any
try:
    import gui
    _gui_import_error: Exception | None = None
except Exception as _e:
    gui = None
    _gui_import_error = _e

def parse_args_and_config() -> tuple[argparse.Namespace, dict[str, Any]]:
    '''Parse the configuration data and command line arguments consumed by this script.'''
    parser, config_data, validators = config.get_arg_parser_and_config_data(
        description='Helps you track airplanes and satellites with a Celestron '
                    'NexStar telescope mount.')

    config.add_arg_hootl(parser, config_data, validators)

    parser.add_argument(
        '--bw', action='store_true',
        help='Make the display black and white (this is useful for '
             'increasing contrast when operating in direct sunlight).')

    parser.add_argument(
        '--white-bg', action='store_true',
        help='Make the display background white (this is useful to read more '
             'easily on dimmer screens when operating in direct sunlight).')

    config.add_arg_location(parser, config_data, validators)
    config.add_arg_alignment(parser, config_data, validators)
    config.add_arg_landmark(parser, config_data, validators)
    config.add_arg_telescope(parser, config_data, validators)
    config.add_arg_mount_mode(parser, config_data, validators)
    config.add_arg_telescope_protocol(parser, config_data, validators)

    parser.add_argument(
        '--web', action=argparse.BooleanOptionalAction, default=bool(config_data.get('web_gui', False)),
        help='Use the browser GUI instead of the OpenGL window '
             '(default from config.yaml: web_gui, otherwise off).')

    parser.add_argument(
        '--web-port', type=int, default=int(config_data.get('web_port', 8765)),
        help='Port of the browser GUI on 127.0.0.1 (default: 8765).')

    parser.add_argument(
        '--no-browser', action='store_true',
        help='With --web, do not open a browser automatically.')

    parser.add_argument(
        '--sbs1', type=str, action='append', default=[],
        help='The host:port of an SBS1 server for airplane data. You can '
             'specify this argument multiple times in order to receive data '
             'from multiple servers. '
             '(default: ' + ', '.join(config_data['sbs1_servers']) + ')')

    args = parser.parse_args()

    config.validate(validators, args)

    if args.sbs1 == []:
        args.sbs1 = config_data['sbs1_servers']

    return args, config_data

class RestartRequested(Exception):
    '''Restart NexPlane with a different location (requested from the browser GUI).'''
    def __init__(self, location: str):
        super().__init__(location)
        self.location = location

def restart_argv(location: str) -> list[str]:
    '''
    Command line for the restart: same invocation, but with the new location, without
    alignment (it depends on the location) and without opening the browser again.
    '''
    drop_with_value = {'--location', '--alignment', '--landmark'}
    out: list[str] = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        name = a.split('=', 1)[0]
        if name in drop_with_value:
            i += 1 if '=' in a else 2
            continue
        out.append(a)
        i += 1
    out += ['--location', location]
    if '--web' not in out:
        out.append('--web')
    if '--no-browser' not in out:
        out.append('--no-browser')
    return [sys.executable, sys.argv[0]] + out

def main() -> None:
    args, config_data = parse_args_and_config()

    # Where are we?
    observatory_location = util.configured_earth_location(config_data, args.location)

    # Instantiate the Sun and Moon so we can draw them in the GUI
    # (and so we can gaurd against accidentally pointing at the Sun).
    sun = astronomical.AstroBody('sun', observatory_location)
    moon = astronomical.AstroBody('moon', observatory_location)

    # Set up a serial interface to the telescope, either a HOOTL one or a real one.
    serial_iface = align.setup_serial_interface(args, observatory_location)

    # Receive airplane data.
    sbs1_receiver = sbs1.Sbs1Receiver(args.sbs1, observatory_location)

    # Determine initial controller gains
    kp = config_data['gains']['kp']
    ki = config_data['gains']['ki']
    kd = config_data['gains']['kd']

    # Build and run the GUI.
    gui_iface: Any
    exit_exceptions: tuple[type[BaseException], ...]
    if args.web:
        import webgui
        exit_exceptions = (webgui.Exit,)
        gui_iface = webgui.WebGui(observatory_location, kp, ki, kd, port=args.web_port,
                                  open_browser=not args.no_browser, mount_mode=args.mount_mode)
        own_places = set(places.load())

        def publish_locations() -> None:
            '''Send all locations and landmarks (config.yaml and places.yaml) to the browser.'''
            locs = config_data['locations']
            gui_iface.set_locations(args.location, {
                name: (float(loc['lat_degrees']), float(loc['lon_degrees']), float(loc['alt_meters']))
                for name, loc in locs.items()},
                {name: str(loc.get('type', 'location')) for name, loc in locs.items()}, own_places)
        publish_locations()
        if config_data.get('gps', True):
            gui_iface.set_gps(gps.GpsReader())
        gui_airplanes = gui_gain_reader = gui_offset_reader = gui_iface
        gui_sun_moon = gui_telescope = gui_comm_warning = gui_iface
    else:
        if gui is None:
            raise SystemExit(f'The OpenGL GUI is not available ({_gui_import_error}). '
                             'Install GLUT or start with --web.')
        exit_exceptions = (gui.Exit,)
        layers: list[Any] = []
        if args.mount_mode == 'eq':
            layers.append(gui.EqFrameLayer(observatory_location))
        layers.append(gui.HorizonLayer())

        gui_airplanes = gui.AirplaneLayer()
        gui_gain_reader = gui.GainReaderLayer(kp, ki, kd)
        gui_offset_reader = gui.OffsetReaderLayer()
        gui_sun_moon = gui.SunMoonLayer()
        gui_telescope = gui.TelescopeLayer()
        gui_comm_warning = gui.CommWarningLayer()

        layers.extend([
            gui_airplanes,
            gui_gain_reader,
            gui_offset_reader,
            gui_sun_moon,
            gui_telescope,
            gui_comm_warning,
        ])

        gui_iface = gui.Gui(args.bw, args.white_bg, layers)

    restart_location: str | None = None
    pad: gamepad.Gamepad | None = None
    track_log: tracking_log.TrackLog | None = None
    telescope: Any = None   # None until the mount answers; the finally block below checks it
    try:
        # Telescope control interface.
        try:
            telescope = align.setup_telescope_interface(args, serial_iface)
        except nexstar.CommError as e:
            msg = Msg('The mount does not answer ({error}). Please check: is the motor controller switched on '
                      'and powered? Is the USB cable plugged in? Was telescope_server (re)started AFTER switching '
                      'on the controller, and on the right port (ls /dev/ttyUSB*)? Is no other software (SynScan '
                      'app) connected? Then restart NexPlane.', error=str(e) or 'empty response')
            if args.web:
                gui_iface.notify(msg)
                time.sleep(1.0)   # Give the browser a moment to fetch the message
            raise SystemExit('\n' + str(msg))

        # Enable axis soft limits if configured in config.yaml.
        limit_guard = axis_limits.install_from_config(telescope, config_data, args.telescope_protocol)
        if limit_guard is not None and args.web:
            limit_guard.notify = gui_iface.notify

        # Tracking controller, sends commands to the telescope.
        target_tracker = tracker.Tracker(telescope, kp, ki, kd, (args.mount_mode == 'altaz'))

        # Keeps track of how many times we've updated the controller gains.
        last_gain_changes = 0

        # Alignment
        if args.landmark:
            assert not args.alignment, '--landmark and --alignment are mutually exclusive.'
            if args.mount_mode == 'altaz':
                azm_cal, alt_cal = align.azm_alt_cal(telescope, args.landmark, config_data, observatory_location)
            else:
                assert args.mount_mode == 'eq'
                ra_cal, dec_cal = align.ra_dec_cal(telescope, args.landmark, config_data, observatory_location)
        elif args.alignment:
            with open(args.alignment) as f:
                alignment_data = yaml.load(f.read(), yaml.Loader)
            if alignment_data['mount_mode'] != args.mount_mode:
                raise Exception('--mount-mode passed to align.py and nexplane.py must match. ' +
                                alignment_data['mount_mode'] + ' != ' + args.mount_mode)
            if args.mount_mode == 'altaz':
                azm_cal = alignment_data['calibration']['azm']
                alt_cal = alignment_data['calibration']['alt']
            else:
                assert args.mount_mode == 'eq'
                ra_cal = alignment_data['calibration']['ra']
                dec_cal = alignment_data['calibration']['dec']
        else:
            # We'll trust that the telescope has been aligned using one of the built in methods.
            if args.mount_mode == 'altaz':
                azm_cal = 0.0
                alt_cal = 0.0
            else:
                assert args.mount_mode == 'eq'
                ra_cal = 0.0
                dec_cal = 0.0

        # Meridian flip (EQ mode with Sky-Watcher mount only).
        pointing: eq_flip.EqPointing | None = None
        limits_data: dict[str, Any] | None = None
        notify: Any = gui_iface.notify if args.web else print   # takes a Text (Msg or str)
        if args.mount_mode == 'eq' and isinstance(telescope, skywatcher.SkyWatcher):
            pointing = eq_flip.EqPointing(
                telescope, ra_cal, dec_cal, limit_guard, notify=notify,
                auto_flip=bool(config_data.get('meridian_flip', True)),
                max_rate_deg_s=float(config_data.get('max_slew_degrees_per_second', 5.0)))
            # Sidereal time in NexPlane's coordinate system (for the polar misalignment model)
            pointing.model.set_lst(reach_map.local_sidereal_time(observatory_location))
            if limit_guard is None:
                notify(Msg('No safe area configured: the automatic meridian flip is off.'))
            else:
                limits_data = {'home_deg': {1: limit_guard.home[0] * 180 / math.pi,
                                            2: limit_guard.home[1] * 180 / math.pi}}
                warning = pointing.check_home(limits_data['home_deg'][2] / 180 * math.pi) \
                    if (args.landmark or args.alignment) else None
                if warning:
                    notify(warning)
        last_target_key: str | None = None

        # Manual control, homing and alignment in the browser GUI.
        manual: eq_flip.ManualControl | None = None
        home_dec_raw: float | None = None
        alignment_file = args.alignment or os.path.join(config.main_config_dir(), 'align.yaml')
        if args.web and pointing is not None:
            if limits_data is not None:
                home = (limits_data['home_deg'][1] / 180 * math.pi, limits_data['home_deg'][2] / 180 * math.pi)
                home_is_taught = True
                home_dec_raw = home[1]
            else:
                assert isinstance(telescope, skywatcher.SkyWatcher)
                home = (telescope._inquire_position(1), telescope._inquire_position(2))
                home_is_taught = False
            manual = eq_flip.ManualControl(telescope, home, home_is_taught,
                                           float(config_data.get('max_slew_degrees_per_second', 5.0)))
            align_targets = [name for name in config_data['locations'] if name != args.location]
            aligned_text: Text = args.landmark or (Msg('from {file}', file=args.alignment) if args.alignment else '')
            gui_iface.enable_manual(Msg('taught home position') if home_is_taught else Msg('position at NexPlane start'),
                                    align_targets, bool(args.landmark or args.alignment), aligned_text)

        # Safe-zone profiles and grid of reachable sky regions (browser).
        reach_worker: reach_map.ReachWorker | None = None
        limit_settings = axis_limits.get_settings(config_data)
        if args.web and limit_guard is not None and limit_settings is not None:
            gui_iface.set_profiles(list(limit_settings['profiles']), limit_guard.profile)
        if args.web and pointing is not None and limit_guard is not None:
            reach_worker = reach_map.ReachWorker(gui_iface.set_reach)

        def request_reach() -> None:
            '''Recompute the blind-area grid in the background (after alignment or a model change).'''
            if reach_worker is not None and pointing is not None:
                raw_ra, raw_dec = telescope.get_ra_dec()
                gui_iface.set_reach_calculating()
                reach_worker.request(pointing, observatory_location, raw_ra, raw_dec)

        if args.landmark or args.alignment:
            request_reach()

        def write_alignment(new_ra_cal: float, new_dec_cal: float) -> None:
            '''Save the calibration in the same format as align.py (loadable with --alignment).'''
            with open(alignment_file, 'w') as f:
                f.write('# Telescope alignment data saved on ' + time.strftime('%Y-%m-%d %H:%M:%S') + '\n')
                yaml.dump({'mount_mode': 'eq', 'calibration': {
                    'ra': float(util.wrap_rad(new_ra_cal, 0)), 'dec': float(util.wrap_rad(new_dec_cal, 0))}}, f)
                if pointing is not None and pointing.model.active:
                    # For information only: the model applies to this setup and this program run.
                    st = pointing.model.status(obs_lat)
                    f.write(f'# Pointing model: {st["points"]} points, residual {st["rms"]:.2f} degrees, '
                            f'polar axis {st["polar_high"]:+.2f} degrees high, {st["polar_east"]:+.2f} degrees east\n')

        # Remember the mount's heading in the home position: on the next start NexPlane is then
        # roughly aligned without a landmark (accuracy about that of the north alignment).
        home_ha_file = os.path.join(config.main_config_dir(), 'home_direction.yaml')

        def home_raw_now() -> tuple[float, float]:
            '''Raw (ra_raw, dec_raw) of the home position now; ra_raw changes with sidereal time.'''
            assert manual is not None
            ra_offset = eq_flip.SIDEREAL_RAD_PER_S * (time.time() - telescope.start_time)
            return ra_offset - manual.home[0], manual.home[1]

        def store_home_ha() -> None:
            '''Save the hour angle of the RA axis at the taught home position (after every alignment).'''
            if manual is None or not manual.home_is_taught or pointing is None:
                return
            ra_raw_h, _ = home_raw_now()
            h = util.wrap_rad(pointing.model.lst() - (ra_raw_h + pointing.ra_cal), -math.pi)
            try:
                with open(home_ha_file, 'w') as f:
                    f.write('# Written by NexPlane: hour angle of the RA axis in the home position.\n'
                            '# Used for rough alignment without a landmark. Delete it if the mount was rebuilt.\n')
                    yaml.dump({'hour_angle_degrees': float(math.degrees(h)),
                               'saved': time.strftime('%Y-%m-%d %H:%M:%S')}, f)
            except OSError as e:
                notify(Msg('The home direction could not be saved: {error}', error=str(e)))

        def rough_align() -> bool:
            '''Rough alignment from the home position (tube pointing at the pole) and the saved home heading.'''
            if manual is None or not manual.home_is_taught or pointing is None or not os.path.exists(home_ha_file):
                return False
            try:
                with open(home_ha_file) as f:
                    h = math.radians(float(yaml.safe_load(f)['hour_angle_degrees']))
            except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
                return False
            ra_raw_h, dec_raw_h = home_raw_now()
            pointing.set_calibration(pointing.model.lst() - ra_raw_h - h, math.pi / 2 - dec_raw_h)
            gui_iface.set_aligned(Msg('roughly from the home position (no landmark)'), rough=True)
            notify(Msg('Roughly aligned from the home position (the mount pointed at the pole when switched on, '
                       'roughly north). Track a first aircraft, centre it and press ✕.'))
            request_reach()
            return True

        # Correction in the camera image, gamepad and log (EQ mount, browser GUI).
        padctl: pad_control.PadControl | None = None
        pad_settings = pad_control.get_settings(config_data)
        if args.web and pointing is not None:
            gui_iface.enable_axis_correction()
            if pad_settings['enabled']:
                pad = gamepad.Gamepad(str(pad_settings['name'] or ''), float(pad_settings['deadzone']),
                                      bool(pad_settings['rumble']), pad_settings['device'])
                padctl = pad_control.PadControl(pad_settings, pad)
            if config_data.get('track_log', True):
                track_log = tracking_log.TrackLog(os.path.join(config.main_config_dir(), 'logs'))
        if manual is not None and not (args.landmark or args.alignment):
            if not rough_align():
                notify(Msg('Not aligned. Align on a landmark, or without a landmark: choose an aircraft as aiming '
                           'target (list or gamepad R1), move it to the image centre with the stick, press ✕.'))
        recent_planes: list[str] = []
        plane_history: list[str] = []
        last_loop_t = time.monotonic()
        max_slew = math.radians(float(config_data.get('max_slew_degrees_per_second', 5.0)))

        def img2ax(dx: float, dy: float) -> tuple[float, float]:
            '''Offset in the camera image (rad) -> offset of the axes (ra_raw, dec_raw).'''
            assert pointing is not None
            d = None
            if not gui_iface.is_aligned():
                # Without alignment: Dec axis orientation from the home position (points to the pole)
                dec_raw_now = pointing.last_raw[1] if pointing.last_raw is not None else telescope.get_ra_dec()[1]
                d = (dec_raw_now - home_dec_raw + math.pi / 2) if home_dec_raw is not None else 0.0
            return pointing.image_to_axes(dx, dy, d)

        def plane_info(hex_id: str | None) -> tuple[str, float | None, float | None]:
            '''Callsign and current (az, el) of an aircraft; empty values if it is unknown.'''
            if hex_id is None or hex_id not in planes:
                return '', None, None
            try:
                ex = planes[hex_id].extrapolate(time.monotonic_ns())
                return ((planes[hex_id].callsign.value or '').strip(),
                        float(unwrap(ex.az.value)), float(unwrap(ex.el.value)))
            except Exception:
                return '', None, None

        obs_lat = float(observatory_location.lat.to_value('rad'))
        sensor_mm = float(config_data.get('sensor_width_mm', 23.5))
        focal_steps = [float(x) for x in config_data.get('focal_lengths_mm', [150, 300, 600])]
        if args.web:
            gui_iface.set_focal_options(focal_steps, float(config_data.get('focal_length_mm', focal_steps[0])))

        def model_text() -> Text:
            '''Short description of the active pointing model, or an empty string.'''
            assert pointing is not None
            st = pointing.model.status(obs_lat)
            if not st['active']:
                return ''
            txt = Msg('Polar error model active, residual {rms:.2f}°.', rms=st['rms'])
            if st['polar_high'] is not None:
                h, e = st['polar_high'], st['polar_east']
                txt += Msg('Polar axis: {h:.1f}° too {updown}, {e:.1f}° too far {eastwest}.',
                           h=abs(h), updown=Msg('high') if h > 0 else Msg('low'),
                           e=abs(e), eastwest=Msg('east') if e > 0 else Msg('west'))
            return txt

        def save_correction(hex_id: str | None) -> bool:
            '''
            "Accept" button: with a tracked plane, a sync point is recorded and the model
            is refitted (with polar misalignment once there are 3 well-distributed points). Without
            a target, the correction is only applied to the alignment as an offset.
            '''
            assert pointing is not None
            raw_ra, raw_dec = telescope.get_ra_dec()
            old_cal = (pointing.ra_cal, pointing.dec_cal)
            corr = (pointing.corr_ra, pointing.corr_dec)
            callsign, p_az, p_el = plane_info(hex_id)
            res: dict[str, Any] = {}
            msg: Msg
            first = not gui_iface.is_aligned() or not pointing.model.points
            if first and (p_az is None or p_el is None):
                if not gui_iface.is_aligned():
                    notify(Msg('Not aligned yet: first choose an aircraft as aiming target (list or R1), move it '
                               'to the image centre with the stick, then ✕.'))
                    return False
            if first and p_az is not None and p_el is not None:
                # First point of this setup (no landmark or after rough alignment): do a full alignment
                ra_t, dec_t = util.altaz_to_radec(p_el, p_az, observatory_location, util.get_current_time())
                pole_err = eq_flip.pole_problem(dec_t)
                if pole_err:
                    notify(pole_err)
                    return False
                new_ra_cal, new_dec_cal, side = eq_flip.calibrate(ra_t, dec_t, raw_ra, raw_dec, home_dec_raw)
                pointing.set_calibration(new_ra_cal, new_dec_cal)
                pointing.start_session(ra_t, dec_t, raw_ra, raw_dec, callsign or hex_id or '',
                                       gui_iface.get_focal(), sensor_mm)
                gui_iface.set_aligned(Msg('aircraft {name} (side {side})', name=callsign or hex_id, side=side))
                store_home_ha()
                msg = Msg('Aligned on {name} (point 1, side {side}). NexPlane now tracks by itself. Centre further '
                          'aircraft in other directions and press ✕; from 3 points on, the polar misalignment is '
                          'included.', name=callsign or hex_id, side=side)
            elif p_az is not None and p_el is not None:
                ra_t, dec_t = util.altaz_to_radec(p_el, p_az, observatory_location, util.get_current_time())
                res = pointing.sync(ra_t, dec_t, raw_ra, raw_dec, callsign or hex_id or '', gui_iface.get_focal(), sensor_mm)
                new_ra_cal, new_dec_cal = pointing.ra_cal, pointing.dec_cal
                n = len(pointing.model.points)
                msg = Msg('Point {n} accepted ({name}).', n=n, name=callsign or hex_id)
                if res.get('full'):
                    msg += model_text()
                else:
                    need = max(0, pointing.model.MIN_POINTS - n)
                    msg += Msg('Zero point adjusted.')
                    if need:
                        msg += Msg('{need} more point(s) in a clearly different direction, then the polar '
                                   'misalignment is included.', need=need)
                    elif res.get('hint'):
                        msg += res['hint']
                    if pointing.model.active:
                        msg += Msg('(The previous polar error model stays active.)')
                if res.get('warn'):
                    msg += Msg('WARNING: {warning}', warning=res['warn'])
                store_home_ha()
            else:
                new_ra_cal, new_dec_cal = pointing.absorb_correction()
                msg = Msg('Correction accepted as an offset (no aircraft tracked, so no sync point for the model; '
                          'RA axis {ra:+.2f}°, Dec axis {dec:+.2f}°).', ra=math.degrees(corr[0]), dec=math.degrees(corr[1]))
            try:
                write_alignment(new_ra_cal, new_dec_cal)
            except OSError as e:
                notify(Msg('The alignment could not be saved: {error}', error=str(e)))
            if track_log is not None:
                st = pointing.model.status(obs_lat)
                try:
                    track_log.sync({'time': time.time(), 'hex': hex_id or '', 'callsign': callsign,
                                    'plane_az': math.degrees(p_az) if p_az is not None else None,
                                    'plane_el': math.degrees(p_el) if p_el is not None else None,
                                    'ra_raw': math.degrees(raw_ra), 'dec_raw': math.degrees(raw_dec),
                                    'ra_cal_before': math.degrees(util.wrap_rad(old_cal[0], 0)),
                                    'dec_cal_before': math.degrees(util.wrap_rad(old_cal[1], 0)),
                                    'corr_ra': math.degrees(corr[0]), 'corr_dec': math.degrees(corr[1]),
                                    'ra_cal_after': math.degrees(util.wrap_rad(new_ra_cal, 0)),
                                    'dec_cal_after': math.degrees(util.wrap_rad(new_dec_cal, 0)),
                                    'points': st['points'], 'model_active': int(st['active']),
                                    'residual': st['rms'], 'polar_high': st['polar_high'],
                                    'polar_east': st['polar_east'], 'cone': st['cone']})
                except OSError:
                    pass
            notify(msg)
            request_reach()
            return True

        def remove_last_point(index: int | None = None) -> bool:
            '''Delete a sync point (default: the last one) and refit the model.'''
            assert pointing is not None
            n = len(pointing.model.points)
            idx = n - 1 if index is None else index
            name = str(pointing.model.points[idx].get('name', '')) if 0 <= idx < n else ''
            res = pointing.remove_point(idx)
            if res is None:
                notify(Msg('No sync point to delete (at least one point remains).'))
                return False
            write_alignment(pointing.ra_cal, pointing.dec_cal)
            notify(Msg('Sync point {n}{name} deleted, {left} point(s) left.', n=idx + 1,
                       name=f' ({name})' if name else '', left=len(pointing.model.points))
                   + (model_text() or Msg('Zero point alignment only.')))
            request_reach()
            return True

        def compass(az: float) -> Msg:
            '''Name of the compass direction (8 directions) for an azimuth.'''
            names = [Msg('north'), Msg('north-east'), Msg('east'), Msg('south-east'),
                     Msg('south'), Msg('south-west'), Msg('west'), Msg('north-west')]
            return names[int(((math.degrees(az) % 360) + 22.5) // 45) % 8]

        def plane_usable(hex_id: str) -> bool:
            '''Can the mount reach the aircraft now on at least one pier side (within the safe area)?'''
            if pointing is None or limit_guard is None:
                return True
            _, p_az, p_el = plane_info(hex_id)
            if p_az is None or p_el is None:
                return False
            ra_t, dec_t = util.altaz_to_radec(p_el, p_az, observatory_location, util.get_current_time())
            raw_ra, raw_dec = telescope.get_ra_dec()
            return any(c.feasible for c in pointing.candidates(ra_t, dec_t, raw_ra, raw_dec).values())

        def select_plane(direction: str, current: str | None) -> bool:
            '''Gamepad R1/L1: track the next nearest reachable aircraft ("next") or the previous one.'''
            min_el = math.radians(float(pad_settings['min_elevation_degrees']))
            new: str | None = None
            if direction == 'next' and not gui_iface.is_aligned():
                new = pad_control.pick_aim(planes, current, recent_planes, min_el)
            elif direction == 'next':
                new = pad_control.pick_next(planes, scope_azm_alt[0], scope_azm_alt[1], current,
                                            recent_planes, min_el, plane_usable)
            else:
                while plane_history and new is None:
                    h = plane_history.pop()
                    if h in planes and h != current:
                        new = h
            if new is None:
                notify(Msg('Gamepad: no further reachable aircraft.') if direction == 'next'
                       else Msg('Gamepad: no previous aircraft.'))
                return False
            res = gui_iface.command('track', {'hex': new})
            if not res.get('ok'):
                notify(Msg('Gamepad: {error}', error=res.get('error')))
                return False
            if direction == 'next' and current is not None:
                plane_history.append(current)
                del plane_history[:-10]
            recent_planes.append(new)
            del recent_planes[:-6]
            callsign, p_az, p_el = plane_info(new)
            if not gui_iface.is_aligned():
                if p_az is not None and p_el is not None:
                    notify(Msg('Gamepad: aiming target {name} – direction {dir}, {el:.0f}° high. Move it to the '
                               'image centre by hand, then ✕.', name=callsign or new, dir=compass(p_az),
                               el=math.degrees(p_el)))
                else:
                    notify(Msg('Gamepad: aiming target {name}. Move it to the image centre by hand, then ✕.',
                               name=callsign or new))
                return True
            if p_el is not None:
                notify(Msg('Gamepad: tracking {name} ({el:.0f}° high).', name=callsign or new, el=math.degrees(p_el)))
            else:
                notify(Msg('Gamepad: tracking {name}.', name=callsign or new))
            return True

        # Main loop
        warn_comm_failure = False
        while True:
            try:
                gui_comm_warning.update_comm_failure(warn_comm_failure)

                time.sleep(0.05)

                # Get current status of airplanes.
                planes = sbs1_receiver.get_planes()

                # Get current telescope position.
                if args.mount_mode == 'altaz':
                    scope_azm_raw, scope_alt_raw = telescope.get_azm_alt()
                    scope_azm_alt = (util.wrap_rad(scope_azm_raw + azm_cal, 0), util.wrap_rad(scope_alt_raw + alt_cal, 0))
                elif pointing is not None:
                    scope_ra, scope_dec, scope_side = pointing.scope()
                    scope_alt, scope_azm = util.radec_to_altaz(scope_ra, scope_dec, observatory_location, util.get_current_time())
                    scope_azm_alt = (scope_azm, scope_alt)
                else:
                    scope_ra_raw, scope_dec_raw = telescope.get_ra_dec()
                    scope_ra = util.wrap_rad(scope_ra_raw + ra_cal, -math.pi)
                    scope_dec = util.wrap_rad(scope_dec_raw + dec_cal, -math.pi)
                    scope_alt, scope_azm = util.radec_to_altaz(scope_ra, scope_dec, observatory_location, util.get_current_time())
                    scope_azm_alt = (scope_azm, scope_alt)

                # Send new data to the to GUI so it can update the drawing.
                gui_sun_moon.update_positions(sun_azm_alt=unwrap(sun.az_el()),
                                              moon_azm_alt=unwrap(moon.az_el()))
                gui_telescope.update_telescope_location(scope_azm_alt)
                if pointing is not None and hasattr(gui_telescope, 'update_side'):
                    gui_telescope.update_side(pointing.last_scope_side, pointing.side, pointing.auto_flip and limit_guard is not None)
                gui_airplanes.update_planes(planes)

                # Receive new user inputs from the GUI.
                tracked_plane_hex_id = gui_airplanes.get_tracked_plane()
                fixed_target = gui_airplanes.get_fixed_target() if hasattr(gui_airplanes, 'get_fixed_target') else None
                target_key = 'fixed' if fixed_target is not None else tracked_plane_hex_id
                if target_key != last_target_key and pointing is not None:
                    pointing.reset()
                last_target_key = target_key
                az_offset, el_offset = gui_offset_reader.get_offsets()
                kp, ki, kd, gain_changes = gui_gain_reader.get_gains()

                # If the user requested a change to the controller gains, update them accordingly.
                if last_gain_changes != gain_changes:
                    print('kp =', kp, 'ki =', ki, 'kd =', kd)
                    target_tracker.set_gains(kp, ki, kd)
                last_gain_changes = gain_changes

                new_location = gui_iface.pop_location_request() if args.web else None
                if new_location is not None:
                    notify(Msg('Changing location to {name}: the mount is stopped and NexPlane restarts.', name=new_location))
                    target_tracker.stop()
                    if manual is not None:
                        manual.stop()
                    time.sleep(2.5)   # let it coast down; both axes must be stopped on restart
                    raise RestartRequested(new_location)

                if args.web and limit_guard is not None:
                    want_off = gui_iface.get_limits_off()
                    if want_off != limit_guard.bypass:
                        if manual is not None:
                            manual.stop()
                        warning = limit_guard.set_bypass(want_off)
                        notify(Msg('WARNING: Safe area DISABLED (at your own risk). Keep a hand on the power switch!')
                               if want_off else Msg('Safe area active again.'))
                        if warning:
                            notify(warning)

                for preq in (gui_iface.pop_place_requests() if args.web else []):
                    if preq['cmd'] == 'place_save':
                        base = {k: v for k, v in config_data['locations'].items() if k not in own_places}
                        err: Msg | None = places.save(preq['name'], preq['type'], preq['lat'], preq['lon'], preq['alt'], base)
                    else:
                        err = places.delete(preq['name'])
                        if err is None and preq['name'] == args.location:
                            err = Msg('The active location cannot be deleted.')
                    if err:
                        notify(Msg('Place not saved: {error}', error=err) if preq['cmd'] == 'place_save'
                               else Msg('Not deleted: {error}', error=err))
                        continue
                    base = {k: v for k, v in config_data['locations'].items() if k not in own_places}
                    own = places.load()
                    own_places.clear()
                    own_places.update(own)
                    config_data['locations'] = {**base, **own}
                    publish_locations()
                    if manual is not None:
                        gui_iface.set_align_targets([n for n in config_data['locations'] if n != args.location])
                    if preq['cmd'] == 'place_save':
                        saved = (Msg('Landmark “{name}” saved ({lat:.5f}, {lon:.5f}, {alt:.0f} m).', **preq)
                                 if preq['type'] == 'landmark' else
                                 Msg('Location “{name}” saved ({lat:.5f}, {lon:.5f}, {alt:.0f} m).', **preq))
                        notify(saved)
                        if preq.get('activate') and preq['type'] == 'location' and preq['name'] != args.location:
                            gui_iface.command('set_location', {'name': preq['name']})
                    else:
                        notify(Msg('“{name}” deleted.', name=preq['name']))

                # Axis position relative to the taught home position (shown in the browser)
                if args.web and limit_guard is not None:
                    axis_pos = (limit_guard.last_pos[1], limit_guard.last_pos[2])
                    if axis_pos[0] is not None and axis_pos[1] is not None:
                        gui_iface.set_axis_rel(math.degrees(float(axis_pos[0]) - limit_guard.home[0]),
                                               math.degrees(float(axis_pos[1]) - limit_guard.home[1]))

                new_profile = gui_iface.pop_profile_request() if (args.web and limit_guard is not None) else None
                if new_profile is not None and limit_guard is not None:
                    target_tracker.stop()
                    if manual is not None:
                        manual.stop()
                    try:
                        warning = limit_guard.set_profile(new_profile, axis_limits.load_profile(config_data, new_profile))
                        gui_iface.set_profiles(list(limit_settings['profiles']) if limit_settings else [new_profile],
                                               limit_guard.profile)
                        if pointing is not None:
                            pointing.reset()
                        notify(Msg('Profile {name} active: {points} outline points, {zones} exclusion zone(s).',
                                   name=new_profile, points=len(limit_guard.poly), zones=len(limit_guard.holes)))
                        if warning:
                            notify(warning)
                        if gui_iface.is_aligned():
                            request_reach()
                    except axis_limits.LimitError as e:
                        notify(Msg('Changing the profile is not possible: {error}', error=of_exception(e)))

                # Correction (browser buttons) and gamepad
                now_m = time.monotonic()
                dt = min(now_m - last_loop_t, 1.0)
                last_loop_t = now_m
                pad_jog: tuple[float, float] | None = None
                if args.web and pointing is not None:
                    for kind, a, b in gui_iface.pop_corr_requests():
                        if kind == 'step':
                            pointing.add_correction(*img2ax(math.radians(a), math.radians(b)))
                        elif kind == 'reset':
                            pointing.reset_correction()
                            notify(Msg('Correction set to 0.'))
                        elif kind == 'save':
                            save_correction(tracked_plane_hex_id)
                        elif kind == 'forget':
                            target_tracker.stop()
                            if manual is not None:
                                manual.stop()
                            pointing.set_calibration(0.0, 0.0)
                            gui_iface.set_unaligned()
                            txt = Msg('Alignment forgotten (points, model and correction deleted). Not aligned.')
                            if a > 0 and os.path.exists(home_ha_file):
                                try:
                                    os.remove(home_ha_file)
                                    txt += Msg('Stored home direction deleted.')
                                except OSError as e:
                                    txt += Msg('The home direction could not be deleted: {error}', error=str(e))
                            notify(txt)
                        elif kind == 'pop':
                            remove_last_point(int(a) if a >= 0 else None)
                if padctl is not None and pad is not None and pointing is not None:
                    tracking_now = (tracked_plane_hex_id is not None or fixed_target is not None) and gui_iface.is_aligned()
                    for press in pad.pop_presses():
                        st = padctl.step(press)
                        if press == 'triangle':
                            gui_iface.command('stop', {})
                            target_tracker.stop()
                            if manual is not None:
                                manual.stop()
                            gui_iface.home_done()
                            tracked_plane_hex_id = None
                            fixed_target = None
                            tracking_now = False
                            notify(Msg('STOP (gamepad).'))
                            pad.rumble(0.9, 0.9, 400)
                        elif press == 'circle':
                            pointing.reset_correction()
                            notify(Msg('Correction set to 0 (gamepad).'))
                            pad.rumble(0.5, 0.5, 90, 2)
                        elif press == 'cross':
                            if save_correction(tracked_plane_hex_id):
                                pad.rumble(0.7, 0.7, 300)
                            else:
                                pad.rumble(1.0, 1.0, 90, 3)
                        elif press == 'options':
                            cur = gui_iface.get_focal()
                            nxt = next((f for f in focal_steps if f > cur + 1), focal_steps[0])
                            gui_iface.command('set_focal', {'mm': nxt})
                            notify(Msg('Focal length for sync points: {mm:.0f} mm (gamepad).', mm=nxt))
                            pad.rumble(0.4, 0.4, 100, focal_steps.index(nxt) + 1)
                        elif press == 'square':
                            if remove_last_point():
                                pad.rumble(0.5, 0.5, 90, 2)
                            else:
                                pad.rumble(1.0, 1.0, 90, 3)
                        elif press in ('r1', 'l1'):
                            if select_plane('next' if press == 'r1' else 'previous', tracked_plane_hex_id):
                                pad.rumble(0.4, 0.4, 120)
                            else:
                                pad.rumble(1.0, 1.0, 90, 3)
                        elif st is not None:
                            pointing.add_correction(*img2ax(*st))
                            pad.rumble(0.25, 0.25, 40)
                    axes = pad.axes()
                    stick = any(axes[k] != 0.0 for k in ('lx', 'ly', 'rx', 'ry'))
                    if tracking_now:
                        cx, cy = padctl.correction_rate(axes)
                        if cx or cy:
                            pointing.add_correction(*img2ax(cx * dt, cy * dt))
                    elif stick and manual is not None:
                        if gui_iface.home_active():
                            gui_iface.home_done()
                            notify(Msg('Go-home cancelled (gamepad).'))
                        jx, jy = padctl.jog_rate(axes)
                        d_ra, d_dec = img2ax(jx, jy)
                        r1, r2 = -d_ra, d_dec          # Axis 1 runs opposite to ra_raw
                        m = max(abs(r1), abs(r2))
                        if m > max_slew:
                            r1, r2 = r1 * max_slew / m, r2 * max_slew / m
                        pad_jog = (r1, r2)
                if args.web and pointing is not None:
                    gui_iface.set_corr_info(
                        {'ra': math.degrees(pointing.corr_ra), 'dec': math.degrees(pointing.corr_dec),
                         'model': pointing.model.status(obs_lat)},
                        None if pad is None else {'connected': pad.connected, 'name': pad.name, 'error': to_json(pad.error)})

                align_request = gui_iface.pop_align_request() if manual is not None else None
                jog = gui_iface.get_jog() if manual is not None else None
                if jog is None:
                    jog = pad_jog
                homing = gui_iface.home_active() if manual is not None else False
                if manual is not None and manual.moving and jog is None and not homing:
                    manual.stop()

                if manual is not None and align_request is not None:
                    # Alignment from the browser GUI: telescope is pointing at align_request.
                    target_tracker.stop()
                    manual.stop()
                    assert pointing is not None
                    try:
                        real_azm, real_alt = align.get_landmark_azm_alt(align_request, config_data, observatory_location)
                        real_ra, real_dec = util.altaz_to_radec(real_alt, real_azm, observatory_location, util.get_current_time())
                        pole_err = eq_flip.pole_problem(real_dec)
                        if pole_err:
                            raise ValueError(pole_err)
                        raw_ra, raw_dec = telescope.get_ra_dec()
                        new_ra_cal, new_dec_cal, align_side = eq_flip.calibrate(real_ra, real_dec, raw_ra, raw_dec, home_dec_raw)
                        pointing.set_calibration(new_ra_cal, new_dec_cal)
                        pointing.start_session(real_ra, real_dec, raw_ra, raw_dec,
                                               align_request[4:] if align_request.startswith('sky:') else align_request,
                                               gui_iface.get_focal(), sensor_mm)
                        write_alignment(new_ra_cal, new_dec_cal)
                        store_home_ha()
                        align_name = align_request[4:] if align_request.startswith('sky:') else align_request
                        gui_iface.set_aligned(Msg('{name} (side {side})', name=align_name, side=align_side))
                        request_reach()
                        done = Msg('Aligned on {name}, side {side}. Saved in {file}.',
                                   name=align_name, side=align_side, file=alignment_file)
                        if home_dec_raw is None:
                            done += Msg('Without a safe area the side cannot be checked, side A assumed.')
                        notify(done)
                    except Exception as e:
                        notify(Msg('Alignment failed: {error}', error=of_exception(e)))
                elif manual is not None and jog is not None:
                    # Manual control (also allowed near the Sun, so the user can drive back out).
                    target_tracker.stop()
                    manual.jog(*jog)
                elif util.azm_alt_ang_dist(scope_azm_alt, unwrap(sun.az_el())) < 20/180*math.pi:
                    # We've strayed into the keep out circle around the Sun! Emergency Stop!
                    # The user can fix this with the hand controller.
                    target_tracker.stop()
                    if manual is not None:
                        manual.stop()
                        gui_iface.home_done()
                    gui_airplanes.stop_tracking()
                    gui_offset_reader.reset_offsets()
                elif manual is not None and homing:
                    target_tracker.stop()
                    if manual.home_step():
                        gui_iface.home_done()
                        notify(Msg('Home position reached.'))
                elif fixed_target is not None and pointing is not None:
                    # Fixed point (pier-side flip test from the browser GUI).
                    fixed_ra, fixed_dec = util.altaz_to_radec(
                        fixed_target['el'] + el_offset, fixed_target['az'] + az_offset,
                        observatory_location, util.get_current_time())
                    if fixed_target['side'] is None:
                        new_side = eq_flip.other_side(pointing.last_scope_side)
                        if pointing.reachable(fixed_ra, fixed_dec, new_side) is False:
                            notify(Msg('Side change test not possible: this direction lies outside the safe area on '
                                       'side {side}. Point the telescope elsewhere, e.g. further east or west, and try '
                                       'again.', side=new_side))
                            target_tracker.stop()
                            gui_airplanes.stop_tracking()
                            continue
                        fixed_target['side'] = new_side
                        gui_airplanes.set_fixed_side(new_side)
                        notify(Msg('Side change test: the point is approached on side {side}.', side=new_side))
                    pointing.command(target_tracker, fixed_ra, fixed_dec, forced_side=fixed_target['side'])
                elif tracked_plane_hex_id is None:
                    # There is no airplane to track, so stop the tracker.
                    target_tracker.stop()
                elif manual is not None and not gui_iface.is_aligned() and tracked_plane_hex_id in planes:
                    # Not aligned yet: the plane is only an aiming target; the user slews to it manually.
                    target_tracker.stop()
                elif tracked_plane_hex_id in planes:
                    # Extrapolate from the last known position and velocity of the plane to estimate the current position.
                    tracked_plane = planes[tracked_plane_hex_id].extrapolate(time.monotonic_ns())

                    # Inform the target tracker of the target position and the current position of the telescope.
                    if args.mount_mode == 'altaz':
                        target_tracker.go(util.wrap_rad(unwrap(tracked_plane.az.value) + az_offset - azm_cal, scope_azm_raw - math.pi),
                                          util.wrap_rad(unwrap(tracked_plane.el.value) + el_offset - alt_cal, scope_alt_raw - math.pi))
                    else:
                        tracked_plane_ra, tracked_plane_dec = util.altaz_to_radec(
                            unwrap(tracked_plane.el.value) + el_offset,
                            unwrap(tracked_plane.az.value) + az_offset,
                            observatory_location,
                            util.get_current_time())
                        if pointing is not None:
                            plane_now = planes[tracked_plane_hex_id]

                            def future_radec(dt: float, plane: Any = plane_now) -> tuple[float, float]:
                                fut = plane.extrapolate(time.monotonic_ns() + int(dt * 1e9))
                                return util.altaz_to_radec(unwrap(fut.el.value) + el_offset,
                                                           unwrap(fut.az.value) + az_offset,
                                                           observatory_location,
                                                           util.get_current_time() + dt * units.s)

                            cand = pointing.command(target_tracker, tracked_plane_ra, tracked_plane_dec, future=future_radec)
                            if track_log is not None and track_log.due():
                                try:
                                    ahead = plane_now.extrapolate(time.monotonic_ns() + 1_000_000_000)
                                    daz = util.wrap_rad(unwrap(ahead.az.value) - unwrap(tracked_plane.az.value), -math.pi)
                                    track_log.write({
                                        'time': time.time(), 'hex': tracked_plane_hex_id,
                                        'callsign': (plane_now.callsign.value or '').strip(),
                                        'plane_az': math.degrees(unwrap(tracked_plane.az.value)),
                                        'plane_el': math.degrees(unwrap(tracked_plane.el.value)),
                                        'range_m': float(unwrap(tracked_plane.range.value)),
                                        'plane_az_rate': math.degrees(daz),
                                        'plane_el_rate': math.degrees(unwrap(ahead.el.value) - unwrap(tracked_plane.el.value)),
                                        'scope_az': math.degrees(scope_azm_alt[0]), 'scope_el': math.degrees(scope_azm_alt[1]),
                                        'side': cand.side,
                                        'ra_raw': math.degrees(unwrap(pointing.last_raw)[0]),
                                        'dec_raw': math.degrees(unwrap(pointing.last_raw)[1]),
                                        'corr_ra': math.degrees(pointing.corr_ra), 'corr_dec': math.degrees(pointing.corr_dec),
                                        'ra_cal': math.degrees(util.wrap_rad(pointing.ra_cal, 0)),
                                        'dec_cal': math.degrees(util.wrap_rad(pointing.dec_cal, 0))})
                                except (OSError, TypeError, ValueError):
                                    pass
                        else:
                            target_tracker.go(util.wrap_rad(tracked_plane_ra - ra_cal, scope_ra_raw - math.pi),
                                              util.wrap_rad(tracked_plane_dec - dec_cal, scope_dec_raw - math.pi))
                else:
                    # If no data is available for the target, stop tracking and inform the GUI of that fact.
                    target_tracker.stop()
                    gui_airplanes.stop_tracking()
                    gui_offset_reader.reset_offsets()

                # If we got to the end of the loop, communication is ok.
                warn_comm_failure = False
                unreliable_comm_count = 0

            except skywatcher.UnreliableCommError:
                unreliable_comm_count += 1
                if unreliable_comm_count > 5:
                    print('Telescope communication lost! Attempting to continue...')
                    warn_comm_failure = True
            except nexstar.CommError:
                print(traceback.format_exc())
                print('Attempting to continue...')
                warn_comm_failure = True
                if args.telescope_protocol == 'skywatcher-mount-head-wifi':
                    # This protocol can have inconsistent delays, wait a moment for
                    # packets in flight to arrive before trying to resume communication.
                    time.sleep(0.10)
            except rpc.RpcConnectionFailure:
                print(traceback.format_exc())
                print('Attempting to continue...')
                warn_comm_failure = True
            except rpc.RpcRemoteException:
                print(traceback.format_exc())
                print('Attempting to continue...')
                warn_comm_failure = True
    except exit_exceptions:
        pass
    except RestartRequested as r:
        restart_location = r.location
    finally:
        # Stop the telescope and clean up.
        if pad is not None:
            pad.close()
        if track_log is not None:
            track_log.close()
        if telescope is not None:
            try:
                telescope.slew_azmalt(0, 0)
            except Exception:
                pass
        sys.stdout.flush()
        sys.stderr.flush()
        serial_iface.close()
        gui_iface.close()
        sbs1_receiver.close()

    if restart_location is not None:
        argv = restart_argv(restart_location)
        print('Restarting:', ' '.join(argv))
        sys.stdout.flush()
        os.execv(argv[0], argv)

if __name__ == '__main__':
    main()
