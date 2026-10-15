'''
Pointing model for the EQ mount, determined anew for each setup.

From alignment points (landmark during alignment, each "apply" with a
centered aircraft) the following are estimated:
  ra_cal, dec_cal   zero points of the two axes (as before)
  ex, ey            polar axis misalignment (small rotations, rad)
  cone              camera/lens not exactly perpendicular to the Dec axis (only with points on both sides)

The polar misalignment differs for every setup. It is therefore only determined from the
points since the last alignment. With 1-2 points (or an unsuitable distribution)
only the zero point is adjusted; from 3 well-distributed points on, the polar misalignment
is included and compensated everywhere in the sky.

Coordinates: hour angle system (x: meridian at the equator, y: east, z: pole). The
misalignment is fixed there because the polar axis is fixed to the Earth.
'''

import math
import time

from typing import Any

from messages import Msg

SIDEREAL_RAD_PER_S = 2 * math.pi / 86164.0905

Vec = tuple[float, float, float]
Mat = tuple[Vec, Vec, Vec]

IDENTITY: Mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

def vec(h: float, d: float) -> Vec:
    cd = math.cos(d)
    return (cd * math.cos(h), -cd * math.sin(h), math.sin(d))

def unvec(v: Vec) -> tuple[float, float]:
    '''Vector -> (hour angle, declination) with |dec| <= 90 degrees.'''
    x, y, z = v
    return math.atan2(-y, x), math.asin(max(-1.0, min(1.0, z)))

def matmul_v(m: Mat, v: Vec) -> Vec:
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])

def matT_v(m: Mat, v: Vec) -> Vec:
    return (m[0][0] * v[0] + m[1][0] * v[1] + m[2][0] * v[2],
            m[0][1] * v[0] + m[1][1] * v[1] + m[2][1] * v[2],
            m[0][2] * v[0] + m[1][2] * v[1] + m[2][2] * v[2])

def rotation(ex: float, ey: float) -> Mat:
    '''Rotation about x (meridian direction, tilts the polar axis east/west), then about y (east, tilts it up/down).'''
    cx, sx = math.cos(ex), math.sin(ex)
    cy, sy = math.cos(ey), math.sin(ey)
    rx = ((1.0, 0.0, 0.0), (0.0, cx, -sx), (0.0, sx, cx))
    ry = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
    return tuple(tuple(sum(rx[i][k] * ry[k][j] for k in range(3)) for j in range(3)) for i in range(3))  # type: ignore[return-value]

def mech_vec(h_m: float, d_m: float, cone: float) -> Vec:
    '''Pointing direction in the mount's axis system; cone shifts perpendicular to Dec motion.'''
    p = vec(h_m, d_m)
    if cone == 0.0:
        return p
    v = (-math.sin(h_m), -math.cos(h_m), 0.0)
    c, s = math.cos(cone), math.sin(cone)
    return (c * p[0] + s * v[0], c * p[1] + s * v[1], c * p[2] + s * v[2])

class Model:
    '''
    Pointing model of one setup: zero points, polar axis misalignment and cone error, fitted to
    sync points (see the module docstring).
    '''
    def __init__(self) -> None:
        self.ex = 0.0
        self.ey = 0.0
        self.cone = 0.0
        self.R: Mat = IDENTITY
        self.active = False           # polar misalignment is compensated
        self.lst0 = 0.0               # Sidereal time (in NexPlane's coordinate system) at time t0
        self.t0 = 0.0
        self.lst_ready = False
        self.points: list[dict[str, Any]] = []
        self.rms = 0.0                # Residual after the fit (rad)
        self.residuals: list[float] = []

    # --- Sidereal time ----------------------------------------------------

    def set_lst(self, lst: float, t: float | None = None) -> None:
        self.lst0 = lst
        self.t0 = time.time() if t is None else t
        self.lst_ready = True

    def lst(self, t: float | None = None) -> float:
        return self.lst0 + SIDEREAL_RAD_PER_S * ((time.time() if t is None else t) - self.t0)

    # --- Conversion sky <-> ideal mount -----------------------------------

    def set_params(self, ex: float, ey: float, cone: float, active: bool) -> None:
        self.ex, self.ey, self.cone, self.active = ex, ey, cone, active
        self.R = rotation(ex, ey) if active else IDENTITY
        if not active:
            self.cone = 0.0

    def to_mount(self, ra: float, dec: float, side_b: bool = False, t: float | None = None) -> tuple[float, float]:
        '''
        True sky direction -> direction (ra, dec) for an ideal mount, on the
        selected side. On side B the cone error has the opposite sign.
        '''
        if not self.active:
            return ra, dec
        lst = self.lst(t)
        q = matT_v(self.R, vec(lst - ra, dec))
        cone = -self.cone if side_b else self.cone
        if cone == 0.0:
            h, d = unvec(q)
        else:
            c, s = math.cos(cone), math.sin(cone)
            d = math.asin(max(-1.0, min(1.0, q[2] / c)))
            a = c * math.cos(d)
            h = math.atan2(-q[1], q[0]) - math.atan2(s, a)
        return lst - h, d

    def from_raw(self, ra_mech: float, d_mech: float, t: float | None = None) -> tuple[float, float]:
        '''
        Mechanical position (ra_mech = ra_raw + ra_cal, d_mech = dec_raw + dec_cal, any
        range) -> true sky direction (ra, dec).
        '''
        lst = self.lst(t)
        p = matmul_v(self.R, mech_vec(lst - ra_mech, d_mech, self.cone))
        h, d = unvec(p)
        return lst - h, d

    # --- Fit -----------------------------------------------------------------

    def _predict(self, params: list[float], pt: dict[str, Any]) -> Vec:
        ra_cal, dec_cal, ex, ey, cone = params
        lst = self.lst(pt['t'])
        h_m = lst - (pt['ra_raw'] + ra_cal)
        d_m = pt['dec_raw'] + dec_cal
        return matmul_v(rotation(ex, ey), mech_vec(h_m, d_m, cone))

    def _residual(self, params: list[float], pt: dict[str, Any]) -> tuple[float, float]:
        '''Pointing error at a sync point for the given parameters: (hour angle, declination) components, rad.'''
        lst = self.lst(pt['t'])
        h_t, d_t = lst - pt['ra'], pt['dec']
        p = self._predict(params, pt)
        t = vec(h_t, d_t)
        # Tangent basis at the target: direction of increasing hour angle and increasing dec
        e_h = (-math.sin(h_t), -math.cos(h_t), 0.0)
        e_d = (-math.sin(d_t) * math.cos(h_t), math.sin(d_t) * math.sin(h_t), math.cos(d_t))
        diff = (p[0] - t[0], p[1] - t[1], p[2] - t[2])
        return (diff[0] * e_h[0] + diff[1] * e_h[1] + diff[2] * e_h[2],
                diff[0] * e_d[0] + diff[1] * e_d[1] + diff[2] * e_d[2])

    def _solve(self, params: list[float], free: list[int], points: list[dict[str, Any]],
               iterations: int = 30) -> tuple[list[float], list[list[float]] | None]:
        '''Gauss-Newton with numerical derivative. Returns parameters and (J^T J)^-1 of the free parameters.'''
        import numpy
        p = list(params)
        cov = None
        for _ in range(iterations):
            r0 = numpy.array([c / self.sigma(pt) for pt in points for c in self._residual(p, pt)])
            J = numpy.zeros((len(r0), len(free)))
            for k, i in enumerate(free):
                q = list(p)
                q[i] += 1e-7
                r1 = numpy.array([c / self.sigma(pt) for pt in points for c in self._residual(q, pt)])
                J[:, k] = (r1 - r0) / 1e-7
            JTJ = J.T @ J
            try:
                step = numpy.linalg.solve(JTJ, -J.T @ r0)
            except numpy.linalg.LinAlgError:
                return p, None
            for k, i in enumerate(free):
                p[i] += float(step[k])
            if float(numpy.max(numpy.abs(step))) < 1e-10:
                break
        try:
            cov = numpy.linalg.inv(JTJ).tolist()
        except numpy.linalg.LinAlgError:
            cov = None
        return p, cov

    @classmethod
    def sigma(cls, pt: dict[str, Any]) -> float:
        '''Accuracy of a point (rad); all points count the same.'''
        return cls.NOISE

    MIN_POINTS = 3
    MAX_STD = math.radians(0.5)       # max. uncertainty of the polar misalignment, otherwise zero point only
    MAX_TILT = math.radians(8.0)      # larger misalignment is implausible (wrongly centered point?)
    NOISE = math.radians(0.1)         # assumed accuracy of an alignment point

    def fit(self, ra_cal: float, dec_cal: float) -> dict[str, Any]:
        '''
        Refit to all points. Returns {'ra_cal', 'dec_cal', 'full'} and optionally 'hint' or 'warn' (Msg).
        Without sufficient points: zero point only from the last point, previous polar misalignment is kept.
        '''
        assert self.points
        latest = self.points[-1]
        result: dict[str, Any] = {'full': False}
        if len(self.points) >= self.MIN_POINTS:
            sides = {('B' if abs(_wrap(pt['dec_raw'] + dec_cal)) > math.pi / 2 else 'A') for pt in self.points}
            free = [0, 1, 2, 3] + ([4] if len(sides) == 2 and len(self.points) >= 4 else [])
            start = [ra_cal, dec_cal, self.ex if self.active else 0.0, self.ey if self.active else 0.0,
                     self.cone if self.active else 0.0]
            p, cov = self._solve(start, free, self.points)
            res = [math.hypot(*self._residual(p, pt)) for pt in self.points]
            rms = math.sqrt(sum(r * r for r in res) / len(res))
            dof = 2 * len(self.points) - len(free)
            chi2 = sum((c / self.sigma(pt)) ** 2 for pt in self.points for c in self._residual(p, pt))
            scale = max(1.0, math.sqrt(chi2 / dof)) if dof > 0 else 1.0
            ok = cov is not None
            std_ex = std_ey = math.inf
            if cov is not None:
                std_ex = scale * math.sqrt(max(0.0, cov[2][2]))
                std_ey = scale * math.sqrt(max(0.0, cov[3][3]))
                ok = std_ex < self.MAX_STD and std_ey < self.MAX_STD
            if ok and max(abs(p[2]), abs(p[3])) > self.MAX_TILT:
                ok = False
                result['warn'] = Msg('The calculated polar misalignment is implausibly large ({tilt:.1f}°), '
                                     'probably a wrongly centred point. Delete the last point?',
                                     tilt=math.degrees(max(abs(p[2]), abs(p[3]))))
            if ok:
                self.set_params(p[2], p[3], p[4], True)
                self.rms = rms
                self.residuals = res
                result.update({'ra_cal': p[0], 'dec_cal': p[1], 'full': True})
                return result
            if 'warn' not in result:
                result['hint'] = Msg('The points are still too close together. For the polar misalignment, '
                                     'accept a point in a clearly different direction.')
        # Zero point only: keep current polar misalignment, last point exact
        start = [ra_cal, dec_cal, self.ex, self.ey, self.cone] if self.active else [ra_cal, dec_cal, 0.0, 0.0, 0.0]
        p, _ = self._solve(start, [0, 1], [latest])
        self.residuals = [math.hypot(*self._residual(p, pt)) for pt in self.points]
        self.rms = math.sqrt(sum(r * r for r in self.residuals) / len(self.residuals))
        result.update({'ra_cal': p[0], 'dec_cal': p[1]})
        return result

    # --- Display ---------------------------------------------------------------

    def polar_error(self, lat: float) -> tuple[float, float] | None:
        '''(too high +, too far east +) in rad, only if the model is active.'''
        if not self.active:
            return None
        a = matmul_v(self.R, (0.0, 0.0, 1.0))
        sl, cl = math.sin(lat), math.cos(lat)
        north = a[0] * -sl + a[2] * cl
        east = a[1]
        up = a[0] * cl + a[2] * sl
        if lat < 0:
            # Southern hemisphere: polar axis points south
            north, east = -north, -east
        alt = math.asin(max(-1.0, min(1.0, up)))
        az = math.atan2(east, north)
        return alt - abs(lat), az

    def status(self, lat: float) -> dict[str, Any]:
        '''Summary for the GUI: number of points, residuals and the polar axis error in degrees.'''
        pe = self.polar_error(lat)
        return {'points': len(self.points), 'active': self.active,
                'list': [{'name': str(pt.get('name', '')),
                          'res': math.degrees(self.residuals[i]) if i < len(self.residuals) else None}
                         for i, pt in enumerate(self.points)],
                'rms': math.degrees(self.rms) if self.points else None,
                'residuals': [math.degrees(r) for r in self.residuals],
                'polar_high': math.degrees(pe[0]) if pe else None,
                'polar_east': math.degrees(pe[1]) if pe else None,
                'cone': math.degrees(self.cone) if self.active else None}

def _wrap(x: float) -> float:
    return (x + math.pi) % (2 * math.pi) - math.pi
