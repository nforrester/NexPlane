'''Where are the stars and planets?'''

import time

from typing import Any

import astropy.time
import astropy.coordinates as coords
import astropy.units as units

def sky_coord_to_az_el(sky_coord: Any, now: astropy.time.Time, observatory: Any) -> tuple[float, float]: # TODO Any
    '''Determine the azimuth and elevation corresponding to the position of an astronomical body.

    sky_coord    The astropy.coordinates.SkyCoord of the astronomical body.
    now          The current time, as an astropy.time.Time.
    observatory  The astropy.coordinates.EarthLocation of your observatory.

    Returns (azimuth, elevation) in radians.
    '''
    altaz = sky_coord.transform_to(
        coords.AltAz(obstime=now, location=observatory))
    return float(altaz.az / units.rad), float(altaz.alt / units.rad)

solar_system_bodies = [
    'sun',
    'mercury',
    'venus',
    'moon',
    'mars',
    'jupiter',
    'saturn',
    'uranus',
    'neptune'
]

class AstroBody:
    '''Makes it easy to repeatedly compute the azimuth and elevation of an astronomical body over time.'''
    def __init__(self, name: str, observatory: Any): # TODO Any
        '''
        name         The name of the astronomical body, to be looked up in astropy's databases.
                     This will not throw an exception if it doesn't exist, but self.az_el() will return None.
        observatory  The astropy.coordinates.EarthLocation of your observatory.
        '''
        self._name = name
        self._observatory = observatory
        self._az_el: tuple[float, float] | None = None
        self._az_el_time: float | None = None

    def az_el(self) -> tuple[float, float] | None:
        '''Returns (azimuth, elevation) in radians, right now. Returns None if the astronomical body does not exist.'''
        now_unix = time.time()

        # For efficiency's sake don't bother recomputing the answer more often than every 3 seconds.
        # Astronomical bodies don't move very quickly.
        if self._az_el_time is not None and self._az_el_time + 3 > now_unix:
            return self._az_el
        self._az_el_time = now_unix

        now = astropy.time.Time(now_unix, format='unix')
        if self._name in solar_system_bodies:
            self._az_el = sky_coord_to_az_el(coords.get_body(self._name, now), now, self._observatory)
        else:
            try:
                target = coords.SkyCoord.from_name(self._name)
                self._az_el = sky_coord_to_az_el(target, now, self._observatory)
            except:
                return None

        return self._az_el
