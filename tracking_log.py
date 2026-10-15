'''
CSV logs for measuring ADS-B latency and pointing errors (EQ mount, browser GUI):
logs/tracking_<time>.csv while tracking, logs/sync_points.csv for every sync point.
'''

import csv
import os
import time

from typing import Any

class TrackLog:
    '''While tracking, writes about 5 rows per second plus all applied corrections.'''
    # Angles in degrees, time as Unix time
    FIELDS = ['time', 'hex', 'callsign', 'plane_az', 'plane_el', 'range_m', 'plane_az_rate', 'plane_el_rate',
              'scope_az', 'scope_el', 'side', 'ra_raw', 'dec_raw', 'corr_ra', 'corr_dec', 'ra_cal', 'dec_cal']
    SYNC_FIELDS = ['time', 'hex', 'callsign', 'plane_az', 'plane_el', 'ra_raw', 'dec_raw',
                   'ra_cal_before', 'dec_cal_before', 'corr_ra', 'corr_dec', 'ra_cal_after', 'dec_cal_after',
                   'points', 'model_active', 'residual', 'polar_high', 'polar_east', 'cone']

    def __init__(self, directory: str, interval_s: float = 0.2):
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime('%Y%m%d_%H%M%S')
        self.path = os.path.join(directory, f'tracking_{stamp}.csv')
        self.sync_path = os.path.join(directory, 'sync_points.csv')
        self.interval = interval_s
        self.last = 0.0
        self.f: Any = None
        self.w: Any = None
        self.lines = 0

    def _open(self) -> None:
        if self.f is None:
            self.f = open(self.path, 'w', newline='')
            self.w = csv.writer(self.f)
            self.w.writerow(self.FIELDS)

    def due(self) -> bool:
        return time.monotonic() - self.last >= self.interval

    def write(self, row: dict[str, Any]) -> None:
        self.last = time.monotonic()
        self._open()
        self.w.writerow([_fmt(row.get(k)) for k in self.FIELDS])
        self.lines += 1
        self.f.flush()

    def sync(self, row: dict[str, Any]) -> None:
        '''Append one sync point to sync_points.csv.'''
        new = not os.path.exists(self.sync_path)
        with open(self.sync_path, 'a', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(self.SYNC_FIELDS)
            w.writerow([_fmt(row.get(k)) for k in self.SYNC_FIELDS])

    def close(self) -> None:
        if self.f is not None:
            self.f.close()
            self.f = None

def _fmt(v: Any) -> str:
    if v is None:
        return ''
    if isinstance(v, float):
        return f'{v:.6f}'
    return str(v)
