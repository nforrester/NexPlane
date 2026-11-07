#!/usr/bin/env python3

'''
Consume a CSV file with fields epoch_utc, az_deg, el_deg, and range_km,
and then pretend an airplane is following that trajectory and emit messages
in approximately SBS-1 format (compatible with sbs1.py).
'''

import argparse
import csv
import datetime
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

def aer_to_earth_location(aer: tuple[float, float, float], reference_location: coords.EarthLocation) -> coords.EarthLocation:
    '''
    Add an Azimuth, Elevation, Range (AER) tuple in radians and meters
    to a reference location to determine the target location.
    '''
    ned = util.aer_to_ned(aer[0], aer[1], aer[2])
    return util.earth_location_plus_ned(reference_location, ned)

def time_and_location_table(filename: str, reference_location: coords.EarthLocation) -> list[tuple[float, tuple[float, float, float]]]:
    '''
    Compute a table of unix timestamp and Azimuth, Elevation, Range (AER) in radians and meters.

    filename:           The trajectory file name.
    reference_location: The location on earth for which the AER coordinates are valid.
    '''
    header = None
    lines = []
    with open(filename, newline='') as f:
        reader = csv.reader(f)
        for row in reader:
            if header is None:
                header = row
            else:
                lines.append(row)
    assert header is not None

    epoch_utc_idx = header.index('epoch_utc')
    az_deg_idx = header.index('az_deg')
    el_deg_idx = header.index('el_deg')
    range_km_idx = header.index('range_km')

    table = []
    for line in lines:
        epoch = datetime.datetime.strptime(line[epoch_utc_idx], '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone(datetime.timedelta(hours=0))).timestamp()
        az_rad = float(line[az_deg_idx]) / 180.0 * math.pi
        el_rad = float(line[el_deg_idx]) / 180.0 * math.pi
        range_m = float(line[range_km_idx]) * 1000
        table.append((epoch, (az_rad, el_rad, range_m)))

    return table

def parse_args_and_config() -> tuple[argparse.Namespace, dict[str, Any]]:
    '''Parse the configuration data and command line arguments consumed by this script.'''
    parser, config_data, validators = config.get_arg_parser_and_config_data(
        description='Consume a CSV file with fields epoch_utc, az_deg, el_deg, and range_km,'
                    'and emit SBS-1 data that nexplane.py can consume in '
                    'order to point at the object on that trajectory.')

    parser.add_argument('name', type=str, help='Name of the target')

    parser.add_argument('filename', type=str, help='The trajectory file name')

    parser.add_argument(
        'reference_location', type=str,
        help='Where are you? Pick a named reference_location from your config file')
    def v(args: argparse.Namespace) -> None:
        if args.reference_location not in config_data['locations']:
            raise Exception('Error, invalid reference_location' + repr(args.reference_location) +
                            '. Valid values are: ' + (', '.join(config_data['locations'].keys())))
    validators.append(v)

    parser.add_argument(
        '--port', type=int, default=40004,
        help='Port to run the SBS-1 server on (default: 40004)')

    args = parser.parse_args()

    config.validate(validators, args)

    return args, config_data

def main() -> None:
    args, config_data = parse_args_and_config()

    reference_location = util.configured_earth_location(config_data, args.reference_location)
    time_location = time_and_location_table(args.filename, reference_location)

    TIME_OFFSET = 0

    next_index = 0
    now = time.time() + TIME_OFFSET
    while next_index < len(time_location) and now >= time_location[next_index][0]:
        next_index += 1

    # Handles all the stuff for distributing the output to the clients on the network.
    server = text_server.TextServer(args.port)

    while next_index < len(time_location) - 1:
        now = time.time() + TIME_OFFSET
        (next_time, aer) = time_location[next_index]
        if now >= next_time:
            this_time = next_time
            next_index += 1
            (next_time, next_aer) = time_location[next_index]
            earth_loc = aer_to_earth_location(aer, reference_location)
            earth_loc_next = aer_to_earth_location(next_aer, reference_location)
            vel_ned = util.ned_between_earth_locations(earth_loc_next, earth_loc) / (next_time - this_time)
            track, _, _ = util.ned_to_aer(vel_ned)
            lon, gdlat, gdalt = earth_loc.to_geodetic('WGS84')

            # Compose and send an SBS-1 message.
            message = 'MSG,3,,,{:06X},,,,,,{},{},{},{},{},{},{},,,,,\n'.format(
                1,
                args.name,
                int(gdalt.to(units.imperial.ft).value),
                (numpy.linalg.norm(numpy.array([vel_ned[0], vel_ned[1], 0]))*(units.m/units.s)).to(units.imperial.kn).value,
                track/math.pi*180,
                gdlat.to(units.deg).value,
                lon.to(units.deg).value,
                (-1*vel_ned[2]*(units.m/units.s)).to(units.imperial.ft/units.min).value)
            server.write(message)
            print(message.strip())
        time.sleep(0.05)

if __name__ == '__main__':
    main()
