#!/usr/bin/env python3

'''The main application for tracking airplanes and satellites with a NexStar telescope.'''

import math
import numpy
import sys
import time

import astropy.time
import astropy.coordinates as coords
import astropy.units as units

import astronomical
import config
import gui
import nexstar
import rpc
import skywatcher
import sbs1
import tracker
import util

def azm_alt_ang_dist(aa1, aa2):
    '''Compute the angle in the sky between two azimuth/elevation directions.'''
    ned1 = util.aer_to_ned(aa1[0], aa1[1], 1.0)
    ned2 = util.aer_to_ned(aa2[0], aa2[1], 1.0)
    return math.acos(numpy.dot(ned1, ned2))

def parse_args_and_config():
    '''Parse the configuration data and command line arguments consumed by this script.'''
    parser, config_data = config.get_arg_parser_and_config_data(
        description='Helps you track airplanes and satellites with a Celestron '
                    'NexStar telescope mount.')

    parser.add_argument(
        '--hootl', dest='run_hootl', action='store_true',
        help='Do not connect to telescope_server.py, but instead run an '
             'internal simulation of the telescope. This is useful for testing.' +
             (' This is the default' if config_data['hootl'] else ''))
    parser.add_argument(
        '--no-hootl', dest='run_hootl', action='store_false',
        help='Opposite of --hootl.' +
             (' This is the default' if not config_data['hootl'] else ''))
    parser.set_defaults(run_hootl=config_data['hootl'])

    parser.add_argument(
        '--bw', action='store_true',
        help='Make the display black and white (this is useful for '
             'increasing contrast when operating in direct sunlight).')

    parser.add_argument(
        '--white-bg', action='store_true',
        help='Make the display background white (this is useful to read more '
             'easily on dimmer screens when operating in direct sunlight).')

    parser.add_argument(
        '--location', type=str, default=config_data['location'],
        help='Where are you? Pick a named location from your config file '
             '(default: ' + config_data['location'] + ')')

    parser.add_argument(
        '--landmark', type=str, default=config_data['landmark'],
        help='If it is not possible to use the telescope\'s internal alignment '
             'functions (perhaps because it is cloudy), you can manually point '
             'the telescope at a location listed in your config file, and then '
             'start this program with the --landmark option specifying where '
             'the telescope is pointed. The offset between the known location '
             'and the telescope\'s reported position will be recorded and '
             'compensated for.')

    parser.add_argument(
        '--telescope', type=str, default=config_data['telescope_server'],
        help='The host:port of the telescope_server.py process, which talks '
             'to the telescope mount '
             '(default: ' + config_data['telescope_server'] + ')')

    parser.add_argument(
        '--mount-mode', type=str, default=config_data['mount_mode'],
        help='Type of telescope mount, either altaz or eq. Default: {}'.format(
            config_data['mount_mode']
        )
    )

    parser.add_argument(
        '--telescope-protocol', type=str, default=config_data['telescope_protocol'],
        help='Which protocol to use to talk to the telescope (default: {})'.format(config_data['telescope_protocol']))

    parser.add_argument(
        '--sbs1', type=str, action='append', default=[],
        help='The host:port of an SBS1 server for airplane data. You can '
             'specify this argument multiple times in order to receive data '
             'from multiple servers. '
             '(default: ' + ', '.join(config_data['sbs1_servers']) + ')')

    args = parser.parse_args()

    if args.sbs1 == []:
        args.sbs1 = config_data['sbs1_servers']

    if args.mount_mode not in ['altaz', 'eq']:
        raise Exception('Error, invalid --mount-mode ' + repr(args.mount_mode) + '. Valid values are "altaz" and "eq".')

    return args, config_data

def main():
    args, config_data = parse_args_and_config()

    # Where are we?
    observatory_location = util.configured_earth_location(config_data, args.location)

    # Instantiate the Sun and Moon so we can draw them in the GUI
    # (and so we can gaurd against accidentally pointing at the Sun).
    sun = astronomical.AstroBody('sun', observatory_location)
    moon = astronomical.AstroBody('moon', observatory_location)

    # Set up a serial interface to the telescope, either a HOOTL one or a real one.
    if args.run_hootl:
        current_time = util.get_current_time()

        serial_iface = nexstar.NexStarSerialHootl(current_time=current_time,
                                                  observatory_location=observatory_location,
                                                  altaz_mode=(args.mount_mode == 'altaz'))
    else:
        serial_iface = nexstar.SerialNetClient(args.telescope)

    # Receive airplane data.
    sbs1_receiver = sbs1.Sbs1Receiver(args.sbs1, observatory_location)

    # Determine initial controller gains
    kp = config_data['gains']['kp']
    ki = config_data['gains']['ki']
    kd = config_data['gains']['kd']

    # Run the GUI.
    gui_iface = gui.Gui(args.bw, args.white_bg, kp, ki, kd, (args.mount_mode == 'eq'), observatory_location)

    while True:
        try:
            # Telescope control interface.
            if args.telescope_protocol == 'nexstar-hand-control':
                telescope = nexstar.NexStar(serial_iface)
            else:
                assert telescope_protocol in ['skywatcher-mount-head-usb' or 'skywatcher-mount-head-eqmod']
                telescope = skywatcher.SkyWatcher(serial_iface)

            # Tracking controller, sends commands to the telescope.
            target_tracker = tracker.Tracker(telescope, kp, ki, kd, (args.mount_mode == 'altaz'))

            # Keeps track of how many times we've updated the controller gains.
            last_gain_changes = 0

            if args.landmark:
                if args.mount_mode == 'eq':
                    raise Exception('--landmark is not supported with --mount-mode=eq')

                # The telescope begins pointed at a known landmark.
                # Record the location of this landmark in the telescope's azimuth/elevation coordinate space.
                landmark = util.configured_earth_location(config_data, args.landmark)
                landmark_aer = util.ned_to_aer(util.ned_between_earth_locations(landmark, observatory_location))
                init_scope_azm, init_scope_alt = telescope.get_precise_azm_alt()
                init_real_azm, init_real_alt, _ = landmark_aer
                azm_cal = util.wrap_rad(init_real_azm - init_scope_azm, 0)
                alt_cal = util.wrap_rad(init_real_alt - init_scope_alt, 0)
            else:
                # We'll trust that the telescope has been aligned using one of the built in methods.
                azm_cal = 0.0
                alt_cal = 0.0

            # Main loop
            while True:
                time.sleep(0.05)

                # Get current status of airplanes.
                planes = sbs1_receiver.get_planes()

                # Get current telescope position.
                if args.mount_mode == 'altaz':
                    scope_azm_raw, scope_alt_raw = telescope.get_precise_azm_alt()
                    scope_azm_alt = (util.wrap_rad(scope_azm_raw + azm_cal, 0), util.wrap_rad(scope_alt_raw + alt_cal, 0))
                else:
                    scope_ra_raw, scope_dec_raw = telescope.get_precise_ra_dec()
                    scope_ra = util.wrap_rad(scope_ra_raw, -math.pi)
                    scope_dec = util.wrap_rad(scope_dec_raw, -math.pi)
                    scope_alt, scope_azm = util.radec_to_altaz(scope_ra, scope_dec, observatory_location, util.get_current_time())
                    scope_azm_alt = (scope_azm, scope_alt)

                # Send new data to the to GUI so it can update the drawing.
                gui_iface.provide_update(scope_azm_alt=scope_azm_alt,
                                         sun_azm_alt=sun.az_el(),
                                         moon_azm_alt=moon.az_el(),
                                         airplanes=planes)

                # Receive new user inputs from the GUI.
                tracked_plane_hex_id, az_offset, el_offset, kp, ki, kd, gain_changes = gui_iface.get_inputs()

                # If the user requested a change to the controller gains, update them accordingly.
                if last_gain_changes != gain_changes:
                    print('kp =', kp, 'ki =', ki, 'kd =', kd)
                    target_tracker.set_gains(kp, ki, kd)
                last_gain_changes = gain_changes

                if azm_alt_ang_dist(scope_azm_alt, sun.az_el()) < 20/180*math.pi:
                    # We've strayed into the keep out circle around the Sun! Emergency Stop!
                    # The user can fix this with the hand controller.
                    target_tracker.stop()
                    gui_iface.stop_tracking()
                elif tracked_plane_hex_id is None:
                    # There is no airplane to track, so stop the tracker.
                    target_tracker.stop()
                elif tracked_plane_hex_id in planes:
                    # Extrapolate from the last known position and velocity of the plane to estimate the current position.
                    tracked_plane = planes[tracked_plane_hex_id].extrapolate(time.monotonic_ns())

                    # Inform the target tracker of the target position and the current position of the telescope.
                    if args.mount_mode == 'altaz':
                        target_tracker.go(util.wrap_rad(tracked_plane.az.value + az_offset - azm_cal, scope_azm_raw - math.pi),
                                          util.wrap_rad(tracked_plane.el.value + el_offset - alt_cal, scope_alt_raw - math.pi))
                    else:
                        tracked_plane_ra, tracked_plane_dec = util.altaz_to_radec(
                            tracked_plane.el.value + el_offset,
                            tracked_plane.az.value + az_offset,
                            observatory_location,
                            util.get_current_time())
                        target_tracker.go(tracked_plane_ra, tracked_plane_dec)
                else:
                    # If no data is available for the target, stop tracking and inform the GUI of that fact.
                    target_tracker.stop()
                    gui_iface.stop_tracking()

        except gui.Exit:
            pass
        except rpc.RpcConnectionFailure:
            print(traceback.format_exc())
            print('Restarting...')
            continue
        finally:
            # Stop the telescope and clean up.
            telescope.slew_azmalt(0, 0)
            sys.stdout.flush()
            sys.stderr.flush()
            serial_iface.close()
            gui_iface.close()
            sbs1_receiver.close()
        return

if __name__ == '__main__':
    main()
