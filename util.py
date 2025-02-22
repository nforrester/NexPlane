'''Assorted utility functions.'''

import math
import numpy
import scipy.spatial
import time

from typing import TypeVar, Any

import astropy.coordinates as coords
import astropy.units as units
import astropy.time

T = TypeVar('T')
IntOrFloat = TypeVar('IntOrFloat', bound = int | float)

def unwrap(x: T | None) -> T:
    assert x is not None
    return x

def assert_float(x: Any) -> float:
    assert isinstance(x, float)
    return x

def assert_int(x: Any) -> int:
    assert isinstance(x, int)
    return x

def clamp(value: IntOrFloat, minimum: IntOrFloat, maximum: IntOrFloat) -> IntOrFloat:
    '''Return value, or minimum or maximum if value falls outside that range on one side or the other.'''
    return min(max(value, minimum), maximum)

def wrap_rad(theta: float, minimum: float) -> float:
    '''
    Add or subtract multiples of 2*pi until the angle
    theta is between minimum and minimum+2*pi.
    '''
    while theta >= minimum + 2 * math.pi:
        theta -= 2 * math.pi
    while theta < minimum:
        theta += 2 * math.pi
    return theta

def ned_to_aer(ned: numpy.ndarray) -> tuple[float, float, float]:
    '''Convert a North East Down (NED) vector to azimuth, elevation, and range.'''
    a = wrap_rad(math.atan2(ned[1], ned[0]), 0)
    e = wrap_rad(math.atan2(-1 * ned[2], math.sqrt(ned[0]**2 + ned[1]**2)), -1 * math.pi)
    r = assert_float(numpy.linalg.norm(ned))
    return a, e, r

def aer_to_ned(a: float, e: float, r: float) -> numpy.ndarray:
    '''Convert azimuth, elevation, and range to a North East Down (NED) vector.'''
    vec = [float(r), 0.0, 0.0]
    vec_elevated = scipy.spatial.transform.Rotation.from_rotvec([0.0, float(e), 0.0]).apply(vec)
    return scipy.spatial.transform.Rotation.from_rotvec([0.0, 0.0, float(a)]).apply(vec_elevated)

def normalize(v: numpy.ndarray) -> numpy.ndarray:
    '''Return the vector, but scaled to length 1.'''
    norm = numpy.linalg.norm(v)
    return v / norm

def ned_unit_vectors_at_earth_location(earth_location: coords.EarthLocation) -> tuple[numpy.ndarray, numpy.ndarray, numpy.ndarray]:
    '''
    Determine the directions of North, East, and Down at the
    specified EarthLocations, in geocentric coordinates.
    '''
    # Determine the longitude, latitude, and altitude.
    lon, gdlat, gdalt = earth_location.to_geodetic('WGS84')

    # This doesn't work near the poles
    assert gdlat / units.rad <  0.99 * math.pi / 2
    assert gdlat / units.rad > -0.99 * math.pi / 2

    # Determine offsets to altitude, latitude, and longitude that correspond
    # to movements of one meter in each direction.
    approx_earth_radius = 6371.0 * units.km
    alt_offset = 1.0 * units.m
    lat_offset = alt_offset / approx_earth_radius * units.rad
    lon_offset = lat_offset * math.cos(gdlat / units.rad)

    # Find locations one meter North, East, and Down from the given location.
    n_location = coords.EarthLocation.from_geodetic(lon, gdlat + lat_offset, gdalt, 'WGS84')
    e_location = coords.EarthLocation.from_geodetic(lon + lon_offset, gdlat, gdalt, 'WGS84')
    d_location = coords.EarthLocation.from_geodetic(lon, gdlat, gdalt - alt_offset, 'WGS84')

    # Subtract the geocentric coordinates of the given location from those of the North, East,
    # and Down locations in order to find geocentric vectors that point in those directions.
    n_unit = normalize(numpy.array([
            (n_location.x - earth_location.x).to(units.m).value,
            (n_location.y - earth_location.y).to(units.m).value,
            (n_location.z - earth_location.z).to(units.m).value,
        ]))

    e_unit = normalize(numpy.array([
            (e_location.x - earth_location.x).to(units.m).value,
            (e_location.y - earth_location.y).to(units.m).value,
            (e_location.z - earth_location.z).to(units.m).value,
        ]))

    d_unit = normalize(numpy.array([
            (d_location.x - earth_location.x).to(units.m).value,
            (d_location.y - earth_location.y).to(units.m).value,
            (d_location.z - earth_location.z).to(units.m).value,
        ]))

    return n_unit, e_unit, d_unit

def ned_between_earth_locations(to_loc: coords.EarthLocation, from_loc: coords.EarthLocation) -> numpy.ndarray:
    '''Compute the position of to_loc in the NED frame of from_loc (both EarthLocation objects).'''
    # Find the position of to_loc relative to from_loc in the geocentric frame.
    f_gc = numpy.array([a.to(units.m).value for a in from_loc.to_geocentric()])
    t_gc = numpy.array([a.to(units.m).value for a in to_loc.to_geocentric()])
    rel_gc = t_gc - f_gc

    # Convert from the geocentric frame to from_loc's NED frame.
    n_unit, e_unit, d_unit = ned_unit_vectors_at_earth_location(from_loc)
    return numpy.array([
            numpy.dot(rel_gc, n_unit),
            numpy.dot(rel_gc, e_unit),
            numpy.dot(rel_gc, d_unit),
        ])

def configured_earth_location(config_data: dict[str, Any], name: str) -> coords.EarthLocation:
    '''Return an EarthLocation for the requested location from the provided config data.'''
    lat = config_data['locations'][name]['lat_degrees']
    lon = config_data['locations'][name]['lon_degrees']
    alt = config_data['locations'][name]['alt_meters']
    return coords.EarthLocation.from_geodetic(lon, lat, alt*units.m, 'WGS84')

def get_current_time() -> astropy.time.Time:
    '''Get the current time as an astropy.time.Time object.'''
    return astropy.time.Time(time.time(), format='unix')

def altaz_to_radec(alt: float, azm: float, observatory_location: coords.EarthLocation, current_time: astropy.time.Time) -> tuple[float, float]:
    '''Converts alt/az coordinates to ra/dec coordinates.'''
    # Get an astropy.coordinates.AltAz and astropy.coordinates.SkyCoord
    # corresponding to the given alt and azm.
    alt_az = coords.AltAz(
        obstime=current_time,
        location=observatory_location,
        az=wrap_rad(azm, -math.pi) * units.rad,
        alt=clamp(wrap_rad(alt, -math.pi), -math.pi/2, math.pi/2) * units.rad)
    sky_coord = alt_az.transform_to(coords.SkyCoord(ra=0*units.rad, dec=0*units.rad))

    # Convert to ra/dec
    ra  = sky_coord.ra.to(units.rad).value
    dec = sky_coord.dec.to(units.rad).value

    return ra, dec

def radec_to_altaz(ra: float, dec: float, observatory_location: coords.EarthLocation, current_time: astropy.time.Time) -> tuple[float, float]:
    '''Converts ra/dec coordinates to alt/az coordinates.'''
    sky_coord = coords.SkyCoord(ra=ra*units.rad, dec=dec*units.rad)
    alt_az = sky_coord.transform_to(coords.AltAz(obstime=current_time, location=observatory_location))

    alt = alt_az.alt.to(units.rad).value
    azm = alt_az.az.to(units.rad).value

    return alt, azm
