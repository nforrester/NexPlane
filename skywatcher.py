'''
This file implements the Sky-Watcher Motor Controller Command Set documented in
https://inter-static.skywatcher.com/downloads/skywatcher_motor_controller_command_set.pdf
'''
# TODO FINISH DOC ME

import copy
import math
import select
import socket
import threading
import time
from dataclasses import dataclass

from nexstar import SerialNetClient, CommError, speak_delay

class SkyWatcherUdpClient:
    # TODO DOC ME
    def __init__(self, host_port):
        # TODO DOC ME
        host, port = host_port.split(':')
        self.host_port = (host, int(port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        self.sock.bind(('0.0.0.0', int(port)+1))

    def speak(self, line):
        self.sock.sendto((line + '\r').encode(), self.host_port)

        # Await a reply, timing out at failure_time.
        failure_time = time.monotonic() + 1.0
        while time.monotonic() < failure_time:
            ready, _, _ = select.select([self.sock], [], [], failure_time - time.monotonic())
            # If we got a reply,
            if ready:
                # receive it,
                data, _ = self.sock.recvfrom(10000)
                # decode it,
                response = data.decode()
                # parse it,
                if len(response) == 0:
                    raise CommError(repr(response))
                if response[0] != '=':
                    raise CommError(repr(response))
                if response[-1] != '\r':
                    raise CommError(repr(response))
                return response[1:-1]
        raise CommError('Timeout waiting for response to ' + repr(line))

    def close(self):
        self.sock.close()


class SkyWatcherUdpServerHootl:
    # TODO DOC ME
    def __init__(self, port):
        # TODO DOC ME
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        self.sock.bind(('0.0.0.0', int(port)))

        self.simulator = SkyWatcherSerialHootl()

    def run(self):
        while True:
            ready, _, _ = select.select([self.sock], [], [], 1.0)
            if ready:
                data, (host, port) = self.sock.recvfrom(10000)

                command = data.decode()
                assert command[-1] == '\r'

                response = self.simulator.speak(command[:-1])

                self.sock.sendto(('=' + response + '\r').encode(), (host, port))


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

class SkyWatcherSerialHootl:
    # TODO DOC ME
    def __init__(self):
        # Configuration
        self.cpr = 9216000
        self.hsr = 1
        self.timer_freq = 16000000
        self.accel = 5.0 / 360 * self.cpr # Counts per second per second
        self.max_rate = 5.0 / 360 * self.cpr # Counts per second

        # Simulator state variables
        self.pos = [None]
        self.pos.append(0x800000) # Counts
        self.pos.append(0x800000)

        self.rate = [None]
        self.rate.append(0.0) # Counts per second
        self.rate.append(0.0)

        self.cmd_rate = [None]
        self.cmd_rate.append(0.0) # Counts per second
        self.cmd_rate.append(0.0)

        self.wish_rate = [None]
        self.wish_rate.append(0.0) # Counts per second
        self.wish_rate.append(0.0)

        self.axis_status = [None]
        for _ in ['ra', 'dec']:
            self.axis_status.append(AxisStatus(
                tracking=False,
                ccw=False,
                fast=False,
                running=False,
                blocked=False,
                init_done=False,
                level_switch_on=False,
            ))

        self.time = 0 # Integer nanoseconds
        self.timestep = int(0.02 * 1e9) # Integer nanoseconds to advance per simulation step.

        # Mutex to lock the state variables.
        self.lock = threading.Lock()

        # Start the simulator thread.
        def run_thread():
            self._run_simulator()
        self.stop_thread = False
        self.thread = threading.Thread(target=run_thread)
        self.thread.start()

    def close(self):
        '''Stop the simulator and join the simulator thread.'''
        self.stop_thread = True
        self.thread.join()

    def __del__(self):
        self.close()

    def _run_simulator(self):
        '''Simulator thread.'''
        wall_time = int(time.time()*1e9)
        while not self.stop_thread:
            # Sleep until the top of the next cycle.
            wall_time += self.timestep
            sleep_time = wall_time - int(time.time()*1e9)
            if sleep_time > 0:
                time.sleep(sleep_time/1e9)

            with self.lock:
                # Advance time
                self.time += self.timestep

                for axis in [1, 2]:
                    self.pos[axis] += int(self.timestep / 1e9 * self.rate[axis])

                    while self.pos[axis] < 0:
                        self.pos[axis] += 0x1000000
                    while self.pos[axis] > 0xffffff:
                        self.pos[axis] -= 0x1000000

                    rate_delta = self.cmd_rate[axis] - self.rate[axis]
                    max_rate_delta = self.accel * self.timestep / 1e9
                    if rate_delta > max_rate_delta:
                        rate_delta = max_rate_delta
                        self.rate[axis] += rate_delta
                    elif rate_delta < -max_rate_delta:
                        rate_delta = -max_rate_delta
                        self.rate[axis] += rate_delta
                    else:
                        self.rate[axis] = self.cmd_rate[axis]

                    running = self.rate[axis] != 0
                    self.axis_status[axis].tracking = running
                    self.axis_status[axis].running = running

    @speak_delay
    def speak(self, command):
        '''Decode and execute a command, then encode and return a response.'''
        # If the simulator thread died, just give up.
        if not self.thread.is_alive():
            sys.exit(1)

        assert len(command) > 0
        assert command[0] == ':'

        with self.lock:
            # Inquire Counts Per Revolution
            if command[1] == 'a':
                assert len(command) == 3
                assert command[2] in '12'
                return encode_int_6(self.cpr)

            # Inquire High Speed Ratio
            if command[1] == 'g':
                assert len(command) == 3
                assert command[2] in '12'
                return encode_int_2(self.hsr)

            # Inquire High Speed Ratio
            if command[1] == 'b':
                assert len(command) == 3
                assert command[2] == '1'
                return encode_int_6(self.timer_freq)

            # Initialization Done
            if command[1] == 'F':
                assert len(command) == 3
                assert command[2] in '12'
                axis = int(command[2])
                self.axis_status[axis].init_done = True
                return ''

            # Inquire Status
            if command[1] == 'f':
                assert len(command) == 3
                assert command[2] in '12'
                axis = int(command[2])
                status = self.axis_status[axis]
                value = 0
                if status.tracking:
                    value = value | 0x100
                if status.ccw:
                    value = value | 0x200
                if status.fast:
                    value = value | 0x400
                if status.running:
                    value = value | 0x010
                if status.blocked:
                    value = value | 0x020
                if status.init_done:
                    value = value | 0x001
                if status.level_switch_on:
                    value = value | 0x002
                return format(value, '03X')

            # Stop Motion
            if command[1] == 'K':
                assert len(command) == 3
                assert command[2] in '12'
                axis = int(command[2])
                self.cmd_rate[axis] = 0.0
                return ''

            # Inquire Position
            if command[1] == 'j':
                assert len(command) == 3
                assert command[2] in '12'
                axis = int(command[2])
                return encode_int_6(self.pos[axis])

            # Set Motion Mode
            if command[1] == 'G':
                assert len(command) == 5
                assert command[2] in '12'
                axis = int(command[2])
                if self.axis_status[axis].running:
                    raise Exception('Illegal to set motion mode while axis in motion.')
                value = decode_int_2(command[3:5])
                if value & 0x10 == 0:
                    raise Exception('GOTO not implemented')
                if value & 0x20 == 0:
                    raise Exception('Slow not implemented')
                if value & 0x40 == 1:
                    raise Exception('Medium not implemented')
                if value & 0x80 == 1:
                    raise Exception('GOTO not implemented')
                if value & 0x02 == 1:
                    raise Exception('South not implemented')
                self.axis_status[axis].ccw = 0 != (value & 0x01)
                self.axis_status[axis].fast = 0 != (value & 0x20)
                return ''

            # Set Step Period
            if command[1] == 'I':
                assert len(command) == 9
                assert command[2] in '12'
                axis = int(command[2])
                value = decode_int_6(command[3:9])
                rate = self.hsr * self.timer_freq / value
                if rate > self.max_rate:
                    rate = self.max_rate
                if self.axis_status[axis].ccw:
                    rate *= -1
                self.wish_rate[axis] = rate
                if self.cmd_rate[axis] != 0:
                    self.cmd_rate[axis] = rate
                return ''

            # Start Motion
            if command[1] == 'J':
                assert len(command) == 3
                assert command[2] in '12'
                axis = int(command[2])
                self.cmd_rate[axis] = self.wish_rate[axis]
                return ''

        raise Exception('Invalid or unimplemented command: "{}"'.format(repr(command)))

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

class SkyWatcher:
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
            raise CommError(repr(response))
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
        ra = -self._inquire_position(1)
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
