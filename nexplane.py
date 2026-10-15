#!/usr/bin/env python3

'''The main application for tracking airplanes and satellites with a NexStar telescope.'''

import argparse
import math
import sys
import time
import traceback
import yaml

import astropy.units as units

from typing import Any

import align
import axis_limits
import eq_flip
import astronomical
import config
import nexstar
import rpc
import skywatcher
import sbs1
import tracker
import util

from messages import Msg
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

                if util.azm_alt_ang_dist(scope_azm_alt, unwrap(sun.az_el())) < 20/180*math.pi:
                    # We've strayed into the keep out circle around the Sun! Emergency Stop!
                    # The user can fix this with the hand controller.
                    target_tracker.stop()
                    gui_airplanes.stop_tracking()
                    gui_offset_reader.reset_offsets()
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

                            pointing.command(target_tracker, tracked_plane_ra, tracked_plane_dec, future=future_radec)
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
    finally:
        # Stop the telescope and clean up.
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

if __name__ == '__main__':
    main()
