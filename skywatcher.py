# TODO DOC ME

import math
from dataclasses import dataclass

from nexstar import SerialNetClient

class SkyWatcherError(Exception):
    '''Raised when the telescope does not respond, or gives an unexpected response.'''
    pass

def encode_int_2(v):
    # TODO DOC ME
    h = format(v, '02X')
    assert len(h) == 2, v
    return h

def decode_int_2(s):
    # TODO DOC ME
    assert len(s) == 2, s
    return int(s, 16)

def encode_int_4(v):
    # TODO DOC ME
    h = format(v, '04X')
    assert len(h) == 4, v
    return h[2:4] + h[0:2]

def decode_int_4(s):
    # TODO DOC ME
    assert len(s) == 4, s
    h = s[2:4] + s[0:2]
    return int(h, 16)

def encode_int_6(v):
    # TODO DOC ME
    h = format(v, '06X')
    assert len(h) == 6, v
    return h[4:6] + h[2:4] + h[0:2]

def decode_int_6(s):
    # TODO DOC ME
    assert len(s) == 6, s
    h = s[4:6] + s[2:4] + s[0:2]
    return int(h, 16)

@dataclass
class AxisStatus:
    # TODO DOC ME
    tracking: bool
    ccw: bool
    fast: bool
    running: bool
    blocked: bool
    init_done: bool
    level_switch_on: bool

class SkyWatcher(object):
    '''The main interface for speaking to a SkyWatcher telescope.

    Call member functions to send commands with arguments in sensible units,
    and they will return replies in sensible units.'''
    def __init__(self, serial_port):
        '''
        The argument is an object that provides a speak() function for talking to the
        telescope in the SkyWatcher motor controller serial communication protocol
        (not the SynScan hand controller protocol). Can be either of
        SerialNetClient or SkyWatcherSerialHootl.
        '''
        self.serial_port = serial_port

        self.cpr = [None]
        self.cpr.append(self._inquire_counts_per_revolution(1))
        self.cpr.append(self._inquire_counts_per_revolution(2))

        self.hsr = [None]
        self.hsr.append(self._inquire_high_speed_ratio(1))
        self.hsr.append(self._inquire_high_speed_ratio(2))

        self.timer_freq = self._inquire_timer_freq()

        self._initialization_done(1)
        self._initialization_done(2)

        statuses = []
        statuses.append(self._inquire_status(1))
        statuses.append(self._inquire_status(2))

        for status in statuses:
            assert not status.running
            assert not status.blocked
            assert status.init_done

        self.rate = [None]
        self.rate.append(0.0)
        self.rate.append(0.0)

    def _speak(self, command, response_len):
        '''Helper function that calls self.serial_port.speak() and validates the response length.'''
        response = self.serial_port.speak(command)
        if len(response) != response_len:
            raise SkyWatcherError(repr(response))
        return response

    def _initialization_done(self, axis):
        # TODO DOC ME
        self._speak(':F' + str(axis), 0)

    def _inquire_status(self, axis):
        # TODO DOC ME
        r = self._speak(':f' + str(axis), 3)
        assert len(r) == 3
        value = int(r, 16)
        return AxisStatus(
            tracking = 0 != (value & 0x100),
            ccw = 0 != (value & 0x200),
            fast = 0 != (value & 0x400),
            running = 0 != (value & 0x010),
            blocked = 0 != (value & 0x020),
            init_done = 0 != (value & 0x001),
            level_switch_on = 0 != (value & 0x002),
        )

    def _inquire_counts_per_revolution(self, axis):
        # TODO DOC ME
        r = self._speak(':a' + str(axis), 6)
        return decode_int_6(r)

    def _inquire_high_speed_ratio(self, axis):
        # TODO DOC ME
        r = self._speak(':g' + str(axis), 2)
        return decode_int_2(r)

    def _inquire_timer_freq(self):
        # TODO DOC ME
        r = self._speak(':b1', 6)
        return decode_int_6(r)

    def _inquire_position(self, axis):
        # TODO DOC ME
        r = self._speak(':j' + str(axis), 6)
        v = decode_int_6(r)
        return v / self.cpr[axis] * 2 * math.pi

    def get_precise_ra_dec(self):
        # TODO DOC ME
        ra = self._inquire_position(1)
        dec = self._inquire_position(2)
        return ra, dec

    def get_precise_azm_alt(self):
        # TODO DOC ME
        azm = self._inquire_position(1)
        alt = self._inquire_position(2)
        return azm, alt

    def _set_motion_mode(self, axis, fast, ccw):
        value = 0x10
        if fast:
            value = value | 0x20
        if ccw:
            value = value | 0x01
        self._speak(':G' + str(axis) + encode_int_2(value), 0)

    def _set_step_period(self, axis, step_period):
        assert step_period >= 0
        step_period = int(step_period)
        if step_period > 0xffffff:
            step_period = 0xffffff
        self._speak(':I' + str(axis) + encode_int_6(step_period), 0)

    def _slew_axis(self, axis, rate):
        if rate == 0 or (self.rate[axis] * rate < 0):
            self._speak(':K' + str(axis), 0)
            self.rate[axis] = 0
            return

        if self.rate[axis] == 0:
            if self._inquire_status(axis).running:
                return
            if rate > 0:
                self._set_motion_mode(axis, True, False)
            else:
                self._set_motion_mode(axis, True, True)

        step_period = self.hsr[axis] * self.timer_freq * 2 * math.pi / abs(rate) / self.cpr[axis]
        self._set_step_period(axis, step_period)

        if self.rate[axis] == 0:
            self._speak(':J' + str(axis), 0)

        self.rate[axis] = rate

    def slew_azm_or_ra(self, rate):
        self._slew_axis(1, rate)

    def slew_alt_or_dec(self, rate):
        self._slew_axis(2, rate)

    def slew_azm(self, rate):
        self.slew_azm_or_ra(rate)

    def slew_alt(self, rate):
        self.slew_alt_or_dec(rate)

    def slew_ra(self, rate):
        self.slew_azm_or_ra(-rate)

    def slew_dec(self, rate):
        self.slew_alt_or_dec(rate)

    def slew_azmalt(self, azm_rate, alt_rate):
        '''Set the Az/Alt slew rates.'''
        self.slew_azm(azm_rate)
        self.slew_alt(alt_rate)

    def slew_radec(self, ra_rate, dec_rate):
        '''Set the RA/Dec slew rates.'''
        self.slew_ra(ra_rate)
        self.slew_dec(dec_rate)

    def set_tracking_mode(self, mode):
        '''Noop, provided for compatibility with NexStar.'''
        pass
