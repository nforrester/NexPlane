'''
Meridian flip for equatorial Sky-Watcher mounts in NexPlane.

For almost every point in the sky there are two axis positions:
  Side A: Dec axis on the side of the pole on which the alignment was done.
  Side B: RA rotated by 180 degrees, Dec axis mirrored beyond the pole.

Using the raw axis positions directly as RA/Dec is only correct on side A. This module
  - computes the telescope position correctly on both sides,
  - determines both axis positions for a target (and for RA the most favorable
    revolution) and checks them against the taught safe area,
  - stays on the current side as long as the target is reachable there, and
    otherwise switches to the other side (automatic flip),
  - can drive to a fixed point on the respective other side for testing.

Model (NexPlane convention): ra_raw, dec_raw = telescope.get_ra_dec(),
d = dec_raw + dec_cal. For d <= 90 degrees, side A applies with dec = d and
ra = ra_raw + ra_cal. Beyond the pole (d > 90 degrees), side B applies with
dec = 180 degrees - d and ra = ra_raw + ra_cal + 180 degrees.
The alignment (align.py / --landmark) must therefore be done on side A.
'''

import math
import time

from typing import Any, Callable

import axis_limits

from messages import Msg, Text

SIDE_A = 'A'
SIDE_B = 'B'
TWO_PI = 2 * math.pi
SIDEREAL_RAD_PER_S = TWO_PI / 86164.0905

def _wrap(x: float, lo: float = -math.pi) -> float:
    return (x - lo) % TWO_PI + lo

def other_side(side: str) -> str:
    return SIDE_B if side == SIDE_A else SIDE_A

def calibrate(ra_true: float, dec_true: float, ra_raw: float, dec_raw: float,
              home_dec_raw: float | None = None) -> tuple[float, float, str]:
    '''
    Alignment on a known point (ra_true, dec_true) at which the telescope
    is currently pointing (ra_raw, dec_raw). Returns (ra_cal, dec_cal, side).

    If the Dec position of the home position (tube toward the pole) is known, the side
    of the pole the telescope is on is detected: on side A, dec_raw is smaller than at
    home (there, an increasing count leads toward the pole), on side B larger. This makes
    alignment work on both sides. Without a home value, side A is
    assumed.
    '''
    if home_dec_raw is not None and dec_raw > home_dec_raw:
        return _wrap(ra_true - math.pi - ra_raw), _wrap(math.pi - dec_true - dec_raw), SIDE_B
    return _wrap(ra_true - ra_raw), _wrap(dec_true - dec_raw), SIDE_A

class ManualControl:
    '''
    Manual control of both axes and move to the home position (for the
    browser UI). All commands go through _slew_axis and thus, if
    configured, through the safe area monitoring.
    '''
    def __init__(self, telescope: Any, home: tuple[float, float], home_is_taught: bool,
                 max_rate_deg_s: float = 5.0):
        self.telescope = telescope
        self.home = home
        self.home_is_taught = home_is_taught
        self.max_rate = math.radians(max_rate_deg_s)
        self.moving = False

    def jog(self, rate1: float, rate2: float) -> None:
        self.telescope._slew_axis(1, rate1)
        self.telescope._slew_axis(2, rate2)
        self.moving = rate1 != 0 or rate2 != 0

    def stop(self) -> None:
        self.telescope._slew_axis(1, 0.0)
        self.telescope._slew_axis(2, 0.0)
        self.moving = False

    def home_step(self) -> bool:
        '''One control step toward home. Returns True when home is reached.'''
        e1 = self.home[0] - self.telescope._inquire_position(1)
        e2 = self.home[1] - self.telescope._inquire_position(2)
        if abs(e1) < math.radians(0.02) and abs(e2) < math.radians(0.02):
            self.stop()
            return True
        # Brake proportionally, limit total speed, small residual speed
        r1, r2 = 1.0 * e1, 1.0 * e2
        speed = math.hypot(r1, r2)
        if speed > self.max_rate:
            r1, r2 = r1 * self.max_rate / speed, r2 * self.max_rate / speed
        min_rate = math.radians(0.05)
        if 0 < abs(r1) < min_rate and abs(e1) >= math.radians(0.02):
            r1 = math.copysign(min_rate, r1)
        if 0 < abs(r2) < min_rate and abs(e2) >= math.radians(0.02):
            r2 = math.copysign(min_rate, r2)
        self.jog(r1, r2)
        return False

class Candidate:
    '''One way to point at a target: pier side, unwrapped raw axis targets and the clearance there.'''
    def __init__(self, side: str, ra_raw: float, dec_raw: float, clearance: float | None):
        self.side = side
        self.ra_raw = ra_raw          # unwrapped, matching the current position
        self.dec_raw = dec_raw
        self.clearance = clearance    # Distance to the boundary of the safe area (rad), None = unknown

    @property
    def feasible(self) -> bool:
        return self.clearance is None or self.clearance >= 0

class EqPointing:
    '''
    Pointing for an equatorial Sky-Watcher mount on both pier sides (see the module docstring).
    Converts between raw axis positions and RA/Dec, chooses the side for each target and drives
    the mount there through Tracker.go_raw().
    '''
    def __init__(self, telescope: Any, ra_cal: float, dec_cal: float,
                 guard: 'axis_limits.AxisLimitGuard | None' = None,
                 notify: Callable[[Text], None] = print,
                 auto_flip: bool = True,
                 max_rate_deg_s: float = 5.0,
                 switch_margin_deg: float = 1.0,
                 min_hold_s: float = 10.0):
        self.telescope = telescope
        self.ra_cal = ra_cal
        self.dec_cal = dec_cal
        self.guard = guard
        self.notify = notify
        self.auto_flip = auto_flip
        self.max_rate = math.radians(max_rate_deg_s)
        self.switch_margin = math.radians(switch_margin_deg)
        self.min_hold_s = min_hold_s
        # Alignment correction in axis direction (rad, like ra_raw/dec_raw). Equivalent to a
        # shift of the calibration and therefore applies to all targets and on both sides.
        self.corr_ra = 0.0
        self.corr_dec = 0.0
        self.side: str | None = None       # selected side for the current target
        self.side_since = 0.0
        self.last_scope_side = SIDE_A
        self.last_raw: tuple[float, float] | None = None   # last read position (ra_raw, dec_raw)
        self.last_raw_t = 0.0

    # ------------------------------------------------------------------
    # Position
    # ------------------------------------------------------------------

    def raw_to_radec(self, ra_raw: float, dec_raw: float) -> tuple[float, float, str]:
        '''Raw axis position -> (ra, dec, side) on the sky.'''
        d = _wrap(dec_raw - self.corr_dec + self.dec_cal, -math.pi)     # -180 .. 180 degrees
        ra = ra_raw - self.corr_ra + self.ra_cal
        if d > math.pi / 2:
            return _wrap(ra + math.pi), math.pi - d, SIDE_B
        if d < -math.pi / 2:
            return _wrap(ra + math.pi), -math.pi - d, SIDE_B
        return _wrap(ra), d, SIDE_A

    def scope(self) -> tuple[float, float, str]:
        '''Current telescope position (ra, dec, side), correct on both sides.'''
        ra_raw, dec_raw = self.telescope.get_ra_dec()
        self.last_raw = (ra_raw, dec_raw)
        self.last_raw_t = time.monotonic()
        ra, dec, side = self.raw_to_radec(ra_raw, dec_raw)
        self.last_scope_side = side
        return ra, dec, side

    def check_home(self, home_dec_raw: float) -> Msg | None:
        '''
        Plausibility of the alignment: in the home position (tube toward the pole)
        d should be 90 degrees. A large deviation indicates an alignment on side B
        or a home position that does not point at the pole.
        '''
        d = math.degrees(_wrap(home_dec_raw + self.dec_cal, -math.pi))
        if abs(d - 90.0) > 15.0:
            return Msg('Check the alignment: the home position gives a declination of {d:.0f} degrees instead of '
                       'about 90. Was the alignment object approached on the mirrored side, or does the home '
                       'position not point at the pole? Please align on the normal side.', d=d)
        return None

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    def _axis_pos(self, ra_raw: float, dec_raw: float) -> tuple[float, float]:
        '''Raw axis position (as in the safe area) for ra_raw/dec_raw.'''
        offset = SIDEREAL_RAD_PER_S * (time.time() - self.telescope.start_time)
        return offset - ra_raw, dec_raw

    def _clearance(self, ra_raw: float, dec_raw: float) -> float | None:
        if self.guard is None:
            return None
        return self.guard.clearance_at(self._axis_pos(ra_raw, dec_raw))

    def candidates(self, ra_t: float, dec_t: float, ra_raw_now: float,
                   dec_raw_now: float | None = None) -> dict[str, Candidate]:
        '''
        Both ways (side A and B) to point at (ra_t, dec_t), each with the axis targets closest to the
        current position and its clearance in the safe area.
        '''
        if dec_raw_now is None:
            _, dec_raw_now = self.telescope.get_ra_dec()
        out = {}
        for side in (SIDE_A, SIDE_B):
            if side == SIDE_A:
                ra_raw = ra_t - self.ra_cal + self.corr_ra
                dec_raw = dec_t - self.dec_cal + self.corr_dec
            else:
                ra_raw = ra_t + math.pi - self.ra_cal + self.corr_ra
                dec_raw = math.pi - dec_t - self.dec_cal + self.corr_dec
            # Calibration values may be stored shifted by full revolutions
            # (align.py stores 0..360 degrees). Therefore place the Dec axis at the nearest
            # equivalent position.
            dec_raw = dec_raw_now + _wrap(dec_raw - dec_raw_now)
            # RA representation near the current position, plus the neighbors one revolution away
            base = ra_raw_now + _wrap(ra_raw - ra_raw_now)
            options = [base, base - TWO_PI, base + TWO_PI]
            best: Candidate | None = None
            for ra_opt in options:
                c = Candidate(side, ra_opt, dec_raw, self._clearance(ra_opt, dec_raw))
                if best is None:
                    best = c
                    continue
                # Reachable variant before unreachable one, otherwise the nearest
                if c.feasible and not best.feasible:
                    best = c
                elif c.feasible == best.feasible and c.feasible and \
                        abs(c.ra_raw - ra_raw_now) < abs(best.ra_raw - ra_raw_now):
                    best = c
                elif not c.feasible and not best.feasible and c.clearance is not None and \
                        best.clearance is not None and c.clearance > best.clearance:
                    best = c
            assert best is not None
            out[side] = best
        return out

    def set_calibration(self, ra_cal: float, dec_cal: float) -> None:
        '''New alignment; an existing correction is discarded.'''
        self.ra_cal = ra_cal
        self.dec_cal = dec_cal
        self.corr_ra = 0.0
        self.corr_dec = 0.0
        self.reset()

    # ------------------------------------------------------------------
    # Correction (keyboard, browser)
    # ------------------------------------------------------------------

    MIN_COS = 0.15   # near the pole the RA axis barely moves the image; limit the gain

    def image_to_axes(self, dx: float, dy: float, d: float | None = None) -> tuple[float, float]:
        '''
        Convert a shift in the camera image (rad on the sky; x = direction of RA motion, y = Dec axis)
        into a shift of the axes (ra_raw, dec_raw). The camera is fixed to the
        Dec axis: a Dec motion always shifts the image the same way, an RA motion is reduced
        by cos(d) and has the opposite sign beyond the pole (side B).
        '''
        if d is None:
            dec_raw = self.last_raw[1] if self.last_raw is not None else self.telescope.get_ra_dec()[1]
            d = _wrap(dec_raw - self.corr_dec + self.dec_cal, -math.pi)
        c = math.cos(d)
        if abs(c) < self.MIN_COS:
            c = math.copysign(self.MIN_COS, c if c != 0 else 1.0)
        return dx / c, dy

    def add_correction(self, d_ra: float, d_dec: float, limit: float = math.radians(20.0)) -> None:
        self.corr_ra = max(-limit, min(limit, self.corr_ra + d_ra))
        self.corr_dec = max(-limit, min(limit, self.corr_dec + d_dec))

    def reset_correction(self) -> None:
        self.corr_ra = 0.0
        self.corr_dec = 0.0

    def reachable(self, ra_t: float, dec_t: float, side: str) -> bool | None:
        '''Is the target within the safe area on the given side? None = unknown (no limits).'''
        if self.guard is None:
            return None
        ra_raw_now, dec_raw_now = self.telescope.get_ra_dec()
        return self.candidates(ra_t, dec_t, ra_raw_now, dec_raw_now)[side].feasible

    def reset(self) -> None:
        '''Call on target change or stop.'''
        self.side = None

    LOOKAHEAD_S = (15.0, 30.0, 60.0)

    def _initial_side(self, cands: dict[str, Candidate], ra_raw_now: float,
                      future: Callable[[float], tuple[float, float]] | None) -> str:
        '''
        Side for a new target: the one on which the target is reachable now and stays
        reachable as long as possible. On a tie, the side the telescope is on.
        '''
        if not self.auto_flip or self.guard is None:
            return self.last_scope_side
        score = {side: (1 if cands[side].feasible else 0) for side in (SIDE_A, SIDE_B)}
        if future is not None:
            for side in (SIDE_A, SIDE_B):
                if not score[side]:
                    continue
                for dt in self.LOOKAHEAD_S:
                    try:
                        ra_f, dec_f = future(dt)
                    except Exception:
                        break
                    if not self.candidates(ra_f, dec_f, ra_raw_now, None)[side].feasible:
                        break
                    score[side] += 1
        current = self.last_scope_side
        other = other_side(current)
        return other if score[other] > score[current] else current

    def choose(self, cands: dict[str, Candidate], forced_side: str | None,
               ra_raw_now: float = 0.0,
               future: Callable[[float], tuple[float, float]] | None = None) -> Candidate:
        '''
        Pick the candidate to drive to: forced_side if given; for a new target the side where it stays
        reachable longest; while tracking, flip only when the current side becomes unreachable.
        '''
        now = time.monotonic()
        if forced_side is not None:
            if self.side != forced_side:
                self.side = forced_side
                self.side_since = now
            return cands[forced_side]
        if self.side is None:
            # New target: choose side by reachability now and in the near future.
            self.side = self._initial_side(cands, ra_raw_now, future)
            self.side_since = now - self.min_hold_s
            if self.side != self.last_scope_side:
                self.notify(Msg('The target is easier to reach on side {side}: the mount changes sides.', side=self.side))
        current, other = cands[self.side], cands[other_side(self.side)]
        if self.auto_flip and self.guard is not None and not current.feasible:
            # After a switch, stay on the new side for a while to avoid flip-flopping.
            held_long_enough = now - self.side_since >= self.min_hold_s
            other_ok = other.clearance is not None and other.clearance >= self.switch_margin
            if other_ok and held_long_enough:
                self.side = other.side
                self.side_since = now
                self.notify(Msg('Meridian flip: changing to side {side}. The target is out of view during the '
                                'rotation.', side=self.side))
                return other
        return current

    def command(self, tracker: Any, ra_t: float, dec_t: float, forced_side: str | None = None,
                future: Callable[[float], tuple[float, float]] | None = None) -> Candidate:
        '''
        Drive the mount to the target (ra_t, dec_t). future(dt) optionally returns the
        predicted target position in dt seconds; it is only needed for side selection
        for a new target. Returns the selected position.
        '''
        # The position was usually just read in scope(); do not query again
        # (each query costs two commands to the controller and slows down the control loop).
        if self.last_raw is not None and time.monotonic() - self.last_raw_t < 0.15:
            ra_raw_now, dec_raw_now = self.last_raw
        else:
            ra_raw_now, dec_raw_now = self.telescope.get_ra_dec()
            self.last_raw = (ra_raw_now, dec_raw_now)
            self.last_raw_t = time.monotonic()
        cand = self.choose(self.candidates(ra_t, dec_t, ra_raw_now, dec_raw_now), forced_side, ra_raw_now, future)
        tracker.go_raw(cand.ra_raw, cand.dec_raw, self.max_rate, (ra_raw_now, dec_raw_now))
        return cand
