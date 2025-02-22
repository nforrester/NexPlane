'''
This file implements the Sky-Watcher Motor Controller Command Set documented in
https://inter-static.skywatcher.com/downloads/skywatcher_motor_controller_command_set.pdf

The main class here is SkyWatcher, which contains all the business logic for encoding
requests to the telescope and decoding responses, and presents a nice interface to
the rest of the program. When you construct a SkyWatcher object you must pass an
object that can take those commands and actually talk to the telescope. This is
useful because there are three different useful ways you might talk to the
telescope:

    SerialNetClient
        The telescope is connected to a computer (possibly a different one).
        Talk to it via an RPC server running on that computer.
        See telescope_server.py.

    SkyWatcherUdpClient
        Communicates with the telescope directly over wifi, using Sky-Watcher's
        protocol. This only works with mounts that have wifi of course, like the
        AZ-GTi, or if you've got a SynScan WiFi Adapter.

    SkyWatcherSerialHootl
        A telescope simulator used for Hardware Out Of The Loop (HOOTL) testing.
        This lets you test the software without the risk of damaging your telescope,
        and without the trouble of setting it up.
'''

import copy
import math
import random
import select
import socket
import threading
import time
import sys
from dataclasses import dataclass

from mount_base import Client, Mount, CommError, speak_delay, TrackingMode
from util import unwrap

class UnreliableCommError(Exception):
    '''Raised when the telescope does not respond, but this may be a fluke.'''
    pass

class SkyWatcherUdpClient(Client):
    # TODO DOC ME
    def __init__(self, host_port: str):
        # TODO DOC ME
        host, port = host_port.split(':')
        self.host_port = (host, int(port))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        self.sock.bind(('0.0.0.0', int(port)+1))

    def speak(self, line: str) -> str:
        # Consume any trash data in the receive buffer that's obviously not
        # a response to the command we're about to issue.
        while True:
            ready, _, _ = select.select([self.sock], [], [], 0)
            if ready:
                self.sock.recvfrom(10000)
            else:
                break

        # Transmit our command.
        self.sock.sendto((line + '\r').encode(), self.host_port)

        # Await a reply, timing out at failure_time.
        failure_time = time.monotonic() + 0.10
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
        raise UnreliableCommError('Timeout waiting for response to ' + repr(line))

    def close(self) -> None:
        self.sock.close()


class SkyWatcherUdpServerHootl:
    # TODO DOC ME
    def __init__(self, port: int):
        # TODO DOC ME
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)
        self.sock.bind(('0.0.0.0', port))

        self.simulator = SkyWatcherSerialHootl()
        self.txn_count = 0

    def run(self) -> None:
        while True:
            ready, _, _ = select.select([self.sock], [], [], 1.0)
            if ready:
                data, (host, port) = self.sock.recvfrom(10000)

                command = data.decode()
                assert command[-1] == '\r'

                response = self.simulator.speak(command[:-1])

                if self.txn_count > 100:
                    # Drop packets with 1% probability.
                    if random.random() < 0.01:
                        continue
                    # Delay packets with 1% probability.
                    if random.random() < 0.01:
                        time.sleep(0.5 * random.random())
                self.txn_count += 1

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

class SkyWatcherSerialHootl(Client):
    # TODO DOC ME
    def __init__(self) -> None:
        # Configuration
        self.cpr = 9216000
        self.hsr = 1
        self.timer_freq = 16000000
        self.accel = 5.0 / 360 * self.cpr # Counts per second per second
        self.max_rate = 5.0 / 360 * self.cpr # Counts per second

        # Simulator state variables
        self.pos = [0]
        self.pos.append(0x800000) # Counts
        self.pos.append(0x800000)

        self.rate = [0.0]
        self.rate.append(0.0) # Counts per second
        self.rate.append(0.0)

        self.cmd_rate = [0.0]
        self.cmd_rate.append(0.0) # Counts per second
        self.cmd_rate.append(0.0)

        self.wish_rate = [0.0]
        self.wish_rate.append(0.0) # Counts per second
        self.wish_rate.append(0.0)

        self.axis_status: list[AxisStatus | None] = [None]
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
        def run_thread() -> None:
            self._run_simulator()
        self.stop_thread = False
        self.thread = threading.Thread(target=run_thread)
        self.thread.start()

    def close(self) -> None:
        '''Stop the simulator and join the simulator thread.'''
        self.stop_thread = True
        self.thread.join()

    def __del__(self) -> None:
        self.close()

    def _run_simulator(self) -> None:
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
                    unwrap(self.axis_status[axis]).tracking = running
                    unwrap(self.axis_status[axis]).running = running

    @speak_delay
    def speak(self, command: str) -> str:
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
                unwrap(self.axis_status[axis]).init_done = True
                return ''

            # Inquire Status
            if command[1] == 'f':
                assert len(command) == 3
                assert command[2] in '12'
                axis = int(command[2])
                status = unwrap(self.axis_status[axis])
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
                if unwrap(self.axis_status[axis]).running:
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
                unwrap(self.axis_status[axis]).ccw = 0 != (value & 0x01)
                unwrap(self.axis_status[axis]).fast = 0 != (value & 0x20)
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
                if unwrap(self.axis_status[axis]).ccw:
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

def encode_int_2(v: int) -> str:
    # TODO DOC ME
    h = format(v, '02X')
    assert len(h) == 2, v
    return h

def decode_int_2(s: str) -> int:
    # TODO DOC ME
    assert len(s) == 2, s
    return int(s, 16)

def encode_int_4(v: int) -> str:
    # TODO DOC ME
    h = format(v, '04X')
    assert len(h) == 4, v
    return h[2:4] + h[0:2]

def decode_int_4(s:str) -> int:
    # TODO DOC ME
    assert len(s) == 4, s
    h = s[2:4] + s[0:2]
    return int(h, 16)

def encode_int_6(v: int) -> str:
    # TODO DOC ME
    h = format(v, '06X')
    assert len(h) == 6, v
    return h[4:6] + h[2:4] + h[0:2]

def decode_int_6(s:str) -> int:
    # TODO DOC ME
    assert len(s) == 6, s
    h = s[4:6] + s[2:4] + s[0:2]
    return int(h, 16)

class PositionFilter:
    # TODO DOC ME
    def __init__(self, label: str):
        self.locked_position: float | None = None
        self.locked_update_time = float('-inf')
        self.proposed_position: float | None = None
        self.proposed_persistence = 0
        self.label = label

    def update(self, new_position: float) -> bool:
        # TODO DOC ME
        now = time.time()
        time_tol = 1.5
        max_degrees_per_second = 5.3
        pos_tol = time_tol * max_degrees_per_second / 180.0 * math.pi

        def update_proposed_lock() -> None:
            if self.proposed_position is not None:
                if abs(new_position - self.proposed_position) < pos_tol:
                    self.proposed_persistence += 1
                else:
                    # TODO REMOVE PRINT
                    print(now, self.label, 'Reset proposed lock.')
                    self.proposed_persistence = 0
            self.proposed_position = new_position

            # Accept the new lock if the proposed lock is persistent.
            if self.proposed_persistence > 40:
                # TODO REMOVE PRINT
                print(now, self.label, 'Accept proposed lock.')
                self.locked_position = new_position
                self.locked_update_time = now
                self.proposed_position = None
                self.proposed_persistence = 0

        if self.locked_position is None:
            # We have no existing lock.
            update_proposed_lock()
            return True
        # We have an existing lock.

        if abs(new_position - self.locked_position) < pos_tol:
            # This is within tolerance, so accept the update.
            self.locked_position = new_position
            self.locked_update_time = now
            return True
        # This is outside tolerance.

        update_proposed_lock()

        # The caller should only accept this new position if the
        # lock wasn't updated recently.
        return now - self.locked_update_time > time_tol

class SkyWatcher(Mount):
    '''The main interface for speaking to a SkyWatcher telescope mount.

    Call member functions to send commands with arguments in sensible units,
    and they will return replies in sensible units.'''
    def __init__(self, serial_port: Client):
        '''
        The argument is an object that provides a speak() function for talking to the
        telescope in the SkyWatcher motor controller serial communication protocol
        (not the SynScan hand controller protocol). Can be either of
        SerialNetClient or SkyWatcherSerialHootl.
        '''
        self.serial_port = serial_port

        self.cpr = dict()
        self.cpr[1] = self._inquire_counts_per_revolution(1)
        self.cpr[2] = self._inquire_counts_per_revolution(2)

        self.hsr = dict()
        self.hsr[1] = self._inquire_high_speed_ratio(1)
        self.hsr[2] = self._inquire_high_speed_ratio(2)

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

        self.rate = dict()
        self.rate[1] = 0.0
        self.rate[2] = 0.0

        self.position_filter = dict()
        self.position_filter[1] = PositionFilter('RA: ')
        self.position_filter[2] = PositionFilter('Dec:')

    def _speak(self, command: str, response_len: int) -> str:
        '''Helper function that calls self.serial_port.speak() and validates the response length.'''
        response = self.serial_port.speak(command)
        if len(response) != response_len:
            raise CommError(repr(response))
        return response

    def _initialization_done(self, axis: int) -> None:
        # TODO DOC ME
        self._speak(':F' + str(axis), 0)

    def _inquire_status(self, axis: int) -> AxisStatus:
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

    def _inquire_counts_per_revolution(self, axis: int) -> int:
        # TODO DOC ME
        r = self._speak(':a' + str(axis), 6)
        return decode_int_6(r)

    def _inquire_high_speed_ratio(self, axis: int) -> int:
        # TODO DOC ME
        r = self._speak(':g' + str(axis), 2)
        return decode_int_2(r)

    def _inquire_timer_freq(self) -> int:
        # TODO DOC ME
        r = self._speak(':b1', 6)
        return decode_int_6(r)

    def _inquire_position(self, axis: int) -> float:
        # TODO DOC ME
        r = self._speak(':j' + str(axis), 6)
        v = decode_int_6(r)
        position = v / self.cpr[axis] * 2 * math.pi
        if self.position_filter[axis].update(position):
            return position
        raise CommError('New position seems wrong: ' + r)

    def get_precise_ra_dec(self) -> tuple[float, float]:
        # TODO DOC ME
        ra = -self._inquire_position(1)
        dec = self._inquire_position(2)
        return ra, dec

    def get_precise_azm_alt(self) -> tuple[float, float]:
        # TODO DOC ME
        azm = self._inquire_position(1)
        alt = self._inquire_position(2)
        return azm, alt

    def _set_motion_mode(self, axis: int, fast: bool, ccw: bool) -> None:
        value = 0x10
        if fast:
            value = value | 0x20
        if ccw:
            value = value | 0x01
        self._speak(':G' + str(axis) + encode_int_2(value), 0)

    def _set_step_period(self, axis: int, step_period: float) -> None:
        assert step_period >= 0
        step_period = int(step_period)
        if step_period > 0xffffff:
            step_period = 0xffffff
        self._speak(':I' + str(axis) + encode_int_6(step_period), 0)

    def _slew_axis(self, axis: int, rate: float) -> None:
        if rate == 0 or (self.rate[axis] * rate < 0):
            self._speak(':K' + str(axis), 0)
            if not self._inquire_status(axis).running:
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

    def slew_azm_or_ra(self, rate: float) -> None:
        self._slew_axis(1, rate)

    def slew_alt_or_dec(self, rate: float) -> None:
        self._slew_axis(2, rate)

    def slew_azm(self, rate: float) -> None:
        self.slew_azm_or_ra(rate)

    def slew_alt(self, rate: float) -> None:
        self.slew_alt_or_dec(rate)

    def slew_ra(self, rate: float) -> None:
        self.slew_azm_or_ra(-rate)

    def slew_dec(self, rate: float) -> None:
        self.slew_alt_or_dec(rate)

    def slew_azmalt(self, azm_rate: float, alt_rate: float) -> None:
        '''Set the Az/Alt slew rates.'''
        self.slew_azm(azm_rate)
        self.slew_alt(alt_rate)

    def slew_radec(self, ra_rate: float, dec_rate: float) -> None:
        '''Set the RA/Dec slew rates.'''
        self.slew_ra(ra_rate)
        self.slew_dec(dec_rate)

    def set_tracking_mode(self, mode: TrackingMode) -> None:
        '''Noop, provided for compatibility with NexStar.'''
        pass
