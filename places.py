'''
User-defined locations and landmarks from the browser UI.

They are stored in places.yaml (next to config.yaml) and merged into "locations"
when the configuration is read. config.yaml itself is not modified.
Entries with type: landmark appear only as alignment targets, not as locations.
'''

import math
import os
import re

from typing import Any

import yaml

from messages import Msg

def path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'places.yaml')

def load() -> dict[str, dict[str, Any]]:
    '''Places created in the browser, from places.yaml: {name: {lat_degrees, lon_degrees, alt_meters, type}}.'''
    try:
        with open(path()) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    locs = data.get('locations') or {}
    return {str(k): dict(v) for k, v in locs.items() if isinstance(v, dict)}

def _write(locs: dict[str, dict[str, Any]]) -> None:
    tmp = path() + '.tmp'
    with open(tmp, 'w') as f:
        f.write('# Written by NexPlane (browser, places editor). Own locations and landmarks.\n')
        yaml.safe_dump({'locations': locs}, f, allow_unicode=True, sort_keys=True)
    os.replace(tmp, path())

NAME_RE = re.compile(r'^[\w .\-()]{1,40}$')   # \w includes letters with accents and umlauts

TYPES = ('location', 'landmark')

def validate(name: str, place_type: str, lat: float, lon: float, alt: float) -> Msg | None:
    '''Error message for an invalid entry, or None.'''
    if not NAME_RE.match(name or '') or name.strip() != name:
        return Msg('Name: 1-40 characters: letters, digits, spaces, . - ( ) _')
    if place_type not in TYPES:
        return Msg('The type must be location or landmark')
    for v in (lat, lon, alt):
        if not math.isfinite(v):
            return Msg('Invalid number')
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return Msg('Latitude -90..90, longitude -180..180')
    if not -500 <= alt <= 9000:
        return Msg('Altitude -500..9000 m')
    return None

def save(name: str, place_type: str, lat: float, lon: float, alt: float, config_locations: dict[str, Any]) -> Msg | None:
    '''Saves/replaces an entry. Returns an error message or None.'''
    err = validate(name, place_type, lat, lon, alt)
    if err:
        return err
    own = load()
    if name in config_locations and name not in own:
        return Msg('"{name}" is already defined in config.yaml and cannot be overwritten here.', name=name)
    own[name] = {'lat_degrees': round(lat, 7), 'lon_degrees': round(lon, 7), 'alt_meters': round(alt, 1), 'type': place_type}
    _write(own)
    return None

def delete(name: str) -> Msg | None:
    '''Delete a place created in the browser. Returns an error message or None.'''
    own = load()
    if name not in own:
        return Msg('"{name}" was not created in the browser (change entries from config.yaml there).', name=name)
    del own[name]
    _write(own)
    return None
