#!/usr/bin/env python3

'''Tool for creating saved alignment files that can be loaded by nexplane.py in lieu of landmark alignment.'''

import argparse
import datetime
import yaml

import astropy.coordinates as coords
import astropy.units as units

from typing import Any

import config
import mount_base
import nexstar
import skywatcher
import util

from util import SKYWATCHER_TELESCOPE_PROTOCOLS

def parse_args_and_config() -> tuple[argparse.Namespace, dict[str, Any]]:
    '''Parse the configuration data and command line arguments consumed by this script.'''
    parser, config_data, validators = config.get_arg_parser_and_config_data(
        description='Tool for creating saved alignment files that can be loaded '
                    'by nexplane.py in lieu of landmark alignment.')

    config.add_arg_hootl(parser, config_data, validators)
    config.add_arg_location(parser, config_data, validators)
    config.add_arg_alignment(parser, config_data, validators)
    config.add_arg_landmark(parser, config_data, validators)
    config.add_arg_telescope(parser, config_data, validators)
    config.add_arg_mount_mode(parser, config_data, validators)
    config.add_arg_telescope_protocol(parser, config_data, validators)

    args = parser.parse_args()

    config.validate(validators, args)

    if not args.landmark:
        raise Exception('--landmark alignment is the purpose of align.py, so you must specify --landmark.')

    if not args.alignment:
        raise Exception('Writing an alignment file is the purpose of align.py, so you must specify --alignment')

    return args, config_data

def setup_serial_interface(args: argparse.Namespace, observatory_location: coords.EarthLocation) -> mount_base.Client:
    '''Set up a serial interface to the telescope, either a HOOTL one or a real one.'''
    if args.run_hootl:
        if args.telescope_protocol == 'nexstar-hand-control':
            current_time = util.get_current_time()

            return nexstar.NexStarSerialHootl(
                current_time=current_time,
                observatory_location=observatory_location,
                altaz_mode=(args.mount_mode == 'altaz'))
        else:
            assert args.telescope_protocol in SKYWATCHER_TELESCOPE_PROTOCOLS
            return skywatcher.SkyWatcherSerialHootl()
    else:
        if args.telescope_protocol == 'skywatcher-mount-head-wifi':
            return skywatcher.SkyWatcherUdpClient(args.telescope)
        else:
            return mount_base.SerialNetClient(args.telescope)

def setup_telescope_interface(args: argparse.Namespace, serial_iface: mount_base.Client) -> mount_base.Mount:
    '''Setup telescope control interface.'''
    if args.telescope_protocol == 'nexstar-hand-control':
        return nexstar.NexStar(serial_iface)
    else:
        assert args.telescope_protocol in SKYWATCHER_TELESCOPE_PROTOCOLS
        return skywatcher.SkyWatcher(serial_iface)

def get_landmark_azm_alt(
        landmark: str,
        config_data: dict[str, Any],
        observatory_location: coords.EarthLocation,
    ) -> tuple[float, float]:
    '''Get the azimuth and altitude of the landmark.'''
    sky_prefix = 'sky:'
    if landmark.startswith(sky_prefix):
        # The telescope begins pointed at a known celestial object.
        # Record the location of this object in the telescope's coordinate space.
        object_name = landmark[len(sky_prefix):]
        solar_system_bodies = [
            'sun',
            'mercury',
            'venus',
            'moon',
            'mars',
            'jupiter',
            'saturn',
            'uranus',
            'neptune',
        ]
        if object_name in solar_system_bodies:
            object_skycoord = coords.get_body(object_name, util.get_current_time(), location=observatory_location)
        else:
            object_skycoord = coords.SkyCoord.from_name(object_name)
        object_altaz = object_skycoord.transform_to(coords.AltAz(obstime=util.get_current_time(), location=observatory_location))
        azm = object_altaz.az.to(units.rad).value
        alt = object_altaz.alt.to(units.rad).value
        return azm, alt
    else:
        # The telescope begins pointed at a known landmark.
        # Record the location of this landmark in the telescope's coordinate space.
        landmark_location = util.configured_earth_location(config_data, landmark)
        landmark_aer = util.ned_to_aer(util.ned_between_earth_locations(landmark_location, observatory_location))
        azm, alt, _ = landmark_aer
        return azm, alt

def azm_alt_cal(
        telescope: mount_base.Mount,
        landmark: str,
        config_data: dict[str, Any],
        observatory_location: coords.EarthLocation,
    ) -> tuple[float, float]:
    '''Calibrate telescope in altaz mode.'''
    init_real_azm, init_real_alt = get_landmark_azm_alt(landmark, config_data, observatory_location)
    init_scope_azm, init_scope_alt = telescope.get_azm_alt()
    azm_cal = util.wrap_rad(init_real_azm - init_scope_azm, 0)
    alt_cal = util.wrap_rad(init_real_alt - init_scope_alt, 0)
    return azm_cal, alt_cal

def ra_dec_cal(
        telescope: mount_base.Mount,
        landmark: str,
        config_data: dict[str, Any],
        observatory_location: coords.EarthLocation,
    ) -> tuple[float, float]:
    '''Calibrate telescope in eq mode.'''
    init_real_azm, init_real_alt = get_landmark_azm_alt(landmark, config_data, observatory_location)
    init_real_ra, init_real_dec = util.altaz_to_radec(init_real_alt, init_real_azm, observatory_location, util.get_current_time())
    init_scope_ra, init_scope_dec = telescope.get_ra_dec()
    ra_cal = util.wrap_rad(init_real_ra - init_scope_ra, 0)
    dec_cal = util.wrap_rad(init_real_dec - init_scope_dec, 0)
    return ra_cal, dec_cal

def main() -> None:
    args, config_data = parse_args_and_config()

    # Where are we?
    observatory_location = util.configured_earth_location(config_data, args.location)

    # Set up a serial interface to the telescope, either a HOOTL one or a real one.
    serial_iface = setup_serial_interface(args, observatory_location)

    # Telescope control interface.
    telescope = setup_telescope_interface(args, serial_iface)

    # Landmark alignment
    cal = dict()
    if args.mount_mode == 'altaz':
        cal['azm'], cal['alt'] = azm_alt_cal(telescope, args.landmark, config_data, observatory_location)
    else:
        assert args.mount_mode == 'eq'
        cal['ra'], cal['dec'] = ra_dec_cal(telescope, args.landmark, config_data, observatory_location)

    # Save data
    data = {
        'mount_mode': args.mount_mode,
        'calibration': cal,
    }

    with open(args.alignment, 'w') as f:
        f.write('# Telescope alignment data saved on ' + str(datetime.datetime.now()) + '\n')
        yaml.dump(data, f)

    # Close the connection to the telescope.
    serial_iface.close()

if __name__ == '__main__':
    main()
