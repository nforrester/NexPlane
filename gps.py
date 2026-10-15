'''
GPS position from the built-in LTE modem (via ModemManager / mmcli), in the background.

Used only by the browser UI: shows the position, warns if the selected
location is far away from it, and can adopt it as the location.
Without a modem or without mmcli the status simply stays "not available".
'''

import math
import re
import shutil
import subprocess
import threading
import time

from typing import Any

from messages import Msg, Text, to_json

def parse_gga(line: str) -> tuple[float, float, float, int, int] | None:
    '''$..GGA -> (latitude, longitude, altitude above sea level, fix quality, satellites) or None without a fix.'''
    f = line.strip().split('*')[0].split(',')
    if len(f) < 10 or not f[0].endswith('GGA') or not f[2] or not f[4]:
        return None
    try:
        q = int(f[6] or 0)
        if q <= 0:
            return None
        lat = int(f[2][:2]) + float(f[2][2:]) / 60
        lon = int(f[4][:3]) + float(f[4][3:]) / 60
        if f[3] == 'S':
            lat = -lat
        if f[5] == 'W':
            lon = -lon
        return lat, lon, float(f[9] or 0), q, int(f[7] or 0)
    except ValueError:
        return None

def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(min(1.0, a)))

class GpsReader:
    '''Polls the GPS position of the WWAN modem through mmcli in a background thread.'''
    def __init__(self, interval_s: float = 5.0):
        self.interval = interval_s
        self.lock = threading.Lock()
        self.fix: dict[str, Any] | None = None
        self.status: Text = Msg('searching …')
        self.stop_flag = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def state(self) -> dict[str, Any]:
        '''{'status': message, 'fix': {'lat', 'lon', 'alt', 'sats', 'age_s'} or None} for the GUI.'''
        with self.lock:
            fix = dict(self.fix) if self.fix else None
            status = self.status
        if fix:
            fix['age_s'] = round(time.time() - fix['t'], 0)
        return {'status': to_json(status), 'fix': fix}

    def close(self) -> None:
        self.stop_flag = True

    def _set(self, status: Text, fix: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.status = status
            if fix is not None:
                self.fix = fix

    def _mmcli(self, *args: str) -> tuple[int, str]:
        try:
            p = subprocess.run(['mmcli', *args], capture_output=True, text=True, timeout=15)
            return p.returncode, p.stdout + p.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            return 1, str(e)

    def _run(self) -> None:
        '''Background thread: find the modem, enable GPS, then read the position every few seconds.'''
        if not shutil.which('mmcli'):
            self._set(Msg('not available (ModemManager/mmcli missing)'))
            return
        modem = None
        enabled = False
        while not self.stop_flag:
            if modem is None:
                code, out = self._mmcli('-L')
                m = re.search(r'/Modem/(\d+)', out)
                if not m:
                    self._set(Msg('no modem found (flight mode?)'))
                    time.sleep(30)
                    continue
                modem = m.group(1)
            if not enabled:
                code, out = self._mmcli('-m', modem, '--location-enable-gps-nmea', '--location-enable-gps-raw')
                if code != 0:
                    detail = out.strip().splitlines()[-1][:100] if out.strip() else ''
                    self._set(Msg('GPS cannot be enabled: {detail}', detail=detail) if detail
                              else Msg('GPS cannot be enabled'))
                    modem = None
                    time.sleep(30)
                    continue
                enabled = True
                self._set(Msg('searching for satellites …'))
            code, out = self._mmcli('-m', modem, '--location-get')
            if code != 0:
                modem, enabled = None, False
                time.sleep(self.interval)
                continue
            fix: dict[str, Any] | None = None
            for line in out.splitlines():
                if '$' in line and 'GGA' in line:
                    g = parse_gga(line[line.index('$'):])
                    if g:
                        fix = {'lat': g[0], 'lon': g[1], 'alt': g[2], 'sats': g[4], 't': time.time()}
            if fix is None:
                m_lat = re.search(r'latitude:\s*([-\d.]+)', out)
                m_lon = re.search(r'longitude:\s*([-\d.]+)', out)
                m_alt = re.search(r'altitude:\s*([-\d.]+)', out)
                if m_lat and m_lon and abs(float(m_lat.group(1))) > 1e-6:
                    fix = {'lat': float(m_lat.group(1)), 'lon': float(m_lon.group(1)),
                           'alt': float(m_alt.group(1)) if m_alt else 0.0, 'sats': None, 't': time.time()}
            if fix:
                self._set(Msg('position available'), fix)
            else:
                with self.lock:
                    has_old = self.fix is not None
                self._set(Msg('last position (currently no fix)') if has_old else Msg('searching for satellites …'))
            time.sleep(self.interval)
