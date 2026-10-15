'''
Sky reachability: for a grid of azimuth and elevation it is determined
whether the mount reaches the point within the safe area, and on
which side of the pole (A, B, both or none).

For an equatorial mount, a fixed direction in the sky (azimuth/elevation)
corresponds to a fixed axis position as long as setup and alignment do not
change. The grid therefore only needs to be recomputed after an alignment
or a profile change.

The conversion azimuth/elevation -> hour angle/declination uses
simple spherical trigonometry (without precession). The deviation from
NexPlane's exact calculation is a few tenths of a degree and irrelevant for
the display; the actual monitoring still computes exactly.
'''

import math
import threading
import time

from typing import Any, Callable

import util

from messages import Msg, Text

AZ_STEP_DEG = 3.0
EL_STEP_DEG = 3.0

def _ha_dec(az: float, el: float, lat: float) -> tuple[float, float]:
    '''Azimuth (from north via east), elevation -> hour angle (positive toward west), declination.'''
    sin_dec = math.sin(el) * math.sin(lat) + math.cos(el) * math.cos(lat) * math.cos(az)
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    ha = math.atan2(-math.sin(az) * math.cos(el),
                    math.cos(lat) * math.sin(el) - math.sin(lat) * math.cos(el) * math.cos(az))
    return ha, dec

def compute(pointing: Any, observatory: Any, ra_raw_now: float, dec_raw_now: float,
            az_step_deg: float = AZ_STEP_DEG, el_step_deg: float = EL_STEP_DEG) -> dict[str, Any]:
    '''
    Returns {'az_step', 'el_step', 'rows'}; rows[j] describes the elevation
    (j + 0.5) * el_step, character i the azimuth (i + 0.5) * az_step:
      '0' not reachable, '1' side A only, '2' side B only, '3' both sides.
    '''
    lat = float(observatory.lat.to_value('rad'))
    # Determine sidereal time via a reference point using NexPlane's exact calculation
    ref_az, ref_el = math.pi, math.radians(45.0)
    ref_ra, _ = util.altaz_to_radec(ref_el, ref_az, observatory, util.get_current_time())
    ref_ha, _ = _ha_dec(ref_az, ref_el, lat)
    lst = ref_ra + ref_ha

    n_az = int(round(360.0 / az_step_deg))
    n_el = int(round(90.0 / el_step_deg))
    rows = []
    for j in range(n_el):
        el = math.radians((j + 0.5) * el_step_deg)
        row = []
        for i in range(n_az):
            az = math.radians((i + 0.5) * az_step_deg)
            ha, dec = _ha_dec(az, el, lat)
            cands = pointing.candidates(lst - ha, dec, ra_raw_now, dec_raw_now)
            code = (1 if cands['A'].feasible else 0) + (2 if cands['B'].feasible else 0)
            row.append(str(code))
        rows.append(''.join(row))
    return {'az_step': az_step_deg, 'el_step': el_step_deg, 'rows': rows}

class ReachWorker:
    '''Computes the grid in the background so the main loop does not stall.'''
    def __init__(self, deliver: Callable[[dict[str, Any] | None, Text], None]):
        self.deliver = deliver
        self.thread: threading.Thread | None = None
        self.pending: tuple[Any, ...] | None = None
        self.lock = threading.Lock()

    def request(self, pointing: Any, observatory: Any, ra_raw_now: float, dec_raw_now: float) -> None:
        with self.lock:
            self.pending = (pointing, observatory, ra_raw_now, dec_raw_now)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def _run(self) -> None:
        '''Compute grids until no request is pending (the newest request wins).'''
        while True:
            with self.lock:
                job, self.pending = self.pending, None
            if job is None:
                return
            t0 = time.time()
            try:
                grid = compute(*job)
                grid['seconds'] = round(time.time() - t0, 1)
                self.deliver(grid, '')
            except Exception as e:
                self.deliver(None, Msg('Calculating the blind areas failed: {error}', error=str(e)))
