#!/usr/bin/env python3

'''
Consume TLE files downloaded from https://celestrak.com/NORAD/elements, compute the
current locations of the satellites described within, and then pretend they are
very fast, very high altitude airplanes and emit messages in approximately SBS-1
format (compatible with sbs1.py).
'''

import argparse
import math
import numpy
import random
import socket
import sys
import threading
import time

from typing import Any

import config
import text_server
import util

import astropy.time
import astropy.units as units
import astropy.coordinates as coords

from sgp4.api import Satrec, SGP4_ERRORS

class SatError(Exception):
    '''If a satellite cannot be modelled for some reason, raise this exception.'''
    pass

class Sat:
    '''Computes the current location of a satellite.'''
    def __init__(self, name: str, one: str, two: str):
        '''
        name: The name of the satellite.
        one:  The first line of the TLE.
        two:  The second line of the TLE.
        '''
        self.name = name
        self.model = Satrec.twoline2rv(one, two)
        self.catalog_num = one[3:8]

    def earth_location(self, time: Any) -> Any: # TODO Any
        '''Compute the EarthLocation of the satellite at the given time.'''
        # I don't have a really great understanding of how this works.
        # I just hacked it together by looking at
        # https://docs.astropy.org/en/stable/coordinates/satellites.html
        # It seems to work, so it must be ok...
        error_code, teme_p, teme_v = self.model.sgp4(time.jd1, time.jd2)
        if error_code != 0:
            raise SatError(SGP4_ERRORS[error_code])
        teme_p = coords.CartesianRepresentation(teme_p * units.km)
        teme_v = coords.CartesianDifferential(teme_v * (units.km/units.s))
        teme = coords.TEME(teme_p.with_differentials(teme_v), obstime=time)
        itrs = teme.transform_to(coords.ITRS(obstime=time))
        return itrs.earth_location

def parse_tle_file(filename: str) -> list[Sat]:
    '''
    Parse a TLE file. Each entry should be three lines
    (the first being the name of the satellite).
    '''
    with open(filename) as f:
        sats = []
        name = None
        one = None
        two = None
        for line in f:
            if name is None:
                name = line.strip()
            elif one is None:
                one = line
                assert(one[0] == '1')
            else:
                assert two is None
                two = line
                assert(two[0] == '2')
                sats.append(Sat(name, one, two))
                name = None
                one = None
                two = None
        return sats

def parse_args_and_config() -> tuple[argparse.Namespace, dict[str, Any]]:
    '''Parse the configuration data and command line arguments consumed by this script.'''
    parser, config_data = config.get_arg_parser_and_config_data(
        description='Consume TLE files that you downloaded from CelesTrak, '
                    'and emit SBS-1 data that nexplane.py can consume in '
                    'order to point at satellites.')

    parser.add_argument(
        '--location', type=str, default=config_data['location'],
        help='Where are you? Pick a named location from your config file '
             '(default: ' + config_data['location'] + ')')

    parser.add_argument(
        '--port', type=int, default=40004,
        help='Port to run the SBS-1 server on (default: 40004)')

    parser.add_argument(
        'tle_files', type=str, nargs='*', default=config_data['tle_files'],
        help='TLE files to consume '
             '(default: ' + ', '.join(config_data['tle_files']) + ')')

    return parser.parse_args(), config_data

def main() -> None:
    args, config_data = parse_args_and_config()

    # Where are we?
    observatory_location = util.configured_earth_location(config_data, args.location)

    # Parse all the TLE files.
    sats_dict = dict()
    for filename in args.tle_files:
        sats = parse_tle_file(filename)
        for sat in sats:
            sats_dict[sat.catalog_num] = sat
    sats = list(sats_dict.values())

    # The time when each satellite should next be updated.
    next_predict_times: list[float] = [0]*len(sats)

    # Handles all the stuff for distributing the output to the clients on the network.
    server = text_server.TextServer(args.port)

    while True:
        time.sleep(0.5)
        for i, sat in enumerate(sats):
            # If it's not time to update this satellite yet, skip it.
            if next_predict_times[i] > time.time():
                continue

            # Compute the current position of the satellite.
            now = util.get_current_time()
            try:
                earth_loc = sat.earth_location(now)
            except SatError as e:
                # If something went wrong, set next_predict_times[i] to far in the future
                # so we don't waste time on this satellite again.
                print(sat.name, e)
                next_predict_times[i] = time.time() + 99999999
                continue

            # Determine how far below the plane of the horizon (in meters) the satellite currently is.
            _, _, dist_below_horizon = util.ned_between_earth_locations(earth_loc, observatory_location)

            # If it's more than 200km below the horizon, delay the next prediction time for
            # a random amount of time between 15 and 60 seconds. We don't want to waste time
            # on it until it's closer to being in view.
            if dist_below_horizon > 200000:
                next_predict_times[i] = time.time() + 15 + 45 * random.random()

            # If it's below the horizon, don't delay the next check, but don't bother
            # finishing the calculation.
            if dist_below_horizon > 0:
                continue

            # Compute where the satellite will be one second from now, and then use that
            # to determine the velocity.
            try:
                earth_loc_next = sat.earth_location(now + 1*units.s)
            except SatError as e:
                # If something went wrong, set next_predict_times[i] to far in the future
                # so we don't waste time on this satellite again.
                next_predict_times[i] = time.time() + 99999999
                continue
            vel_ned = util.ned_between_earth_locations(earth_loc_next, earth_loc)
            track, _, _ = util.ned_to_aer(vel_ned)

            # Determine latitude, longitude, and altitude.
            lon, gdlat, gdalt = earth_loc.to_geodetic('WGS84')

            # Compose and send an SBS-1 message.
            message = 'MSG,3,,,{:06X},,,,,,{},{},{},{},{},{},{},,,,,\n'.format(
                i,
                sat.name,
                int(gdalt.to(units.imperial.ft).value),
                (numpy.linalg.norm(numpy.array([vel_ned[0], vel_ned[1], 0]))*(units.m/units.s)).to(units.imperial.kn).value,
                track/math.pi*180,
                gdlat.to(units.deg).value,
                lon.to(units.deg).value,
                (-1*vel_ned[2]*(units.m/units.s)).to(units.imperial.ft/units.min).value)
            server.write(message)

if __name__ == '__main__':
    main()
