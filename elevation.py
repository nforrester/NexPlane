'''
Terrain elevation above sea level for a point from online elevation models (for location selection in the browser).

1. OpenTopoData, EU-DEM 25 m (Europe only, more accurate)
2. Open-Meteo, Copernicus GLO-90 (worldwide, 90 m)
Returns the ground elevation; for landmarks (church tower etc.) the building height must be added.
'''

import json
import threading
import time
import urllib.parse
import urllib.request

from typing import Any

from messages import Msg, Text, join, to_json

_cache: dict[tuple[float, float], dict[str, Any]] = {}
_lock = threading.Lock()
_last_otd = 0.0   # public OpenTopoData instance: at most 1 request per second

def _get(url: str, timeout: float = 6.0) -> Any:
    req = urllib.request.Request(url, headers={'User-Agent': 'NexPlane (location picker)'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def lookup(lat: float, lon: float) -> dict[str, Any]:
    '''{'ok': True, 'alt': meters, 'source': name of the elevation model} or {'ok': False, 'error': message}'''
    global _last_otd
    key = (round(lat, 5), round(lon, 5))
    with _lock:
        if key in _cache:
            return _cache[key]
    errors: list[Text] = []
    try:
        with _lock:
            wait = 1.05 - (time.monotonic() - _last_otd)
        if wait > 0:
            time.sleep(wait)
        with _lock:
            _last_otd = time.monotonic()
        d = _get('https://api.opentopodata.org/v1/eudem25m?' + urllib.parse.urlencode({'locations': f'{lat},{lon}'}))
        alt = d.get('results', [{}])[0].get('elevation')
        if d.get('status') == 'OK' and alt is not None:
            res = {'ok': True, 'alt': float(alt), 'source': 'EU-DEM 25 m (OpenTopoData)'}
            with _lock:
                _cache[key] = res
            return res
        errors.append(Msg('EU-DEM: no data (outside Europe?)'))
    except Exception as e:
        errors.append(f'OpenTopoData: {e}')
    try:
        d = _get('https://api.open-meteo.com/v1/elevation?' + urllib.parse.urlencode({'latitude': lat, 'longitude': lon}))
        alt = (d.get('elevation') or [None])[0]
        if alt is not None:
            res = {'ok': True, 'alt': float(alt), 'source': 'Copernicus GLO-90 (Open-Meteo)'}
            with _lock:
                _cache[key] = res
            return res
        errors.append(Msg('Open-Meteo: no data'))
    except Exception as e:
        errors.append(f'Open-Meteo: {e}')
    return {'ok': False, 'error': to_json(join(errors))}
