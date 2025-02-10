'''
This file implements the NexStar serial communication protocol documented in
https://s3.amazonaws.com/celestron-site-support-files/support_files/1154108406_nexstarcommprot.pdf

The main class here is NexStar, which contains all the business logic for encoding
requests to the telescope and decoding responses, and presents a nice interface to
the rest of the program. When you construct a NexStar object you must pass an
object that can take those commands and actually talk to the telescope. This is
useful because there are two different useful ways you might talk to the
telescope:

    SerialNetClient
        The telescope is connected to a computer (possibly a different one).
        Talk to it via an RPC server running on that computer.
        See telescope_server.py.

    NexStarSerialHootl
        A telescope simulator used for Hardware Out Of The Loop (HOOTL) testing.
        This lets you test the software without the risk of damaging your telescope,
        and without the trouble of setting it up.
'''

import ast
import enum
import math
import serial
import socket
import sys
import threading
import time

import astropy.time
import astropy.coordinates as coords
import astropy.units as units

import rpc
import util

def wrap_b24(theta, minimum):
    '''Wrap an angle expressed in units of 1/(2**24) turns into the range minimum to (minimum + 2**24).'''
    while theta >= minimum + 2**24:
        theta -= 2**24
    while theta < minimum:
        theta += 2**24
    return theta

def rad_to_b24(radians):
    '''Convert an angle in radians to the 24 bit representation the NexStar serial protocol likes.'''
    return util.clamp(int(util.wrap_rad(radians, 0) / (2*math.pi) * (2**24)), 0, 0xffffff)

def b24_to_rad(b24):
    '''Convert an angle in the 24 bit representation the NexStar serial protocol likes to radians.'''
    return b24/(2**24)*2*math.pi

def quarterarcseconds_to_rad(quarterarcseconds):
    '''Convert an angle in quarter arcseconds to radians.'''
    quarterarcseconds_per_turn = 360 * 60 * 60 * 4
    return quarterarcseconds / quarterarcseconds_per_turn * 2 * math.pi

def rad_to_quarterarcseconds(rad):
    '''Convert an angle in radians to quarter arcseconds.'''
    quarterarcseconds_per_turn = 360 * 60 * 60 * 4
    return int(rad / (2 * math.pi) * quarterarcseconds_per_turn)

class TrackingMode(enum.Enum):
    '''Tracking modes the telescope can use.'''
    OFF      = 0
    ALT_AZ   = 1
    # Do not use equatorial tracking modes
    #EQ_NORTH = 2
    #EQ_SOUTH = 3

def to_hex(num_digits, value):
    '''Convert an int to a hexadecimal string, with enough leading zeros so that it has exactly the specified number of digits.'''
    assert value < 16**num_digits
    return '%0*X' % (num_digits, value)

def from_hex(hex_text):
    '''Convert a hexadecimal string to an integer.'''
    return int(hex_text, 16)

def b24_to_hex4(b24):
    '''Convert a 24 bit angle to a 4 digit hex string (this involves a loss in precision).'''
    return to_hex(4, wrap_b24(b24, 0) >> 8)

def b24_to_hex8(b24):
    '''Convert a 24 bit angle to an 8 digit hex string (the two least significant digits get set to zero).'''
    return to_hex(8, wrap_b24(b24, 0) << 8)

SIDERIAL_RATE_RADIANS_PER_SECOND = 7.2921150e-5

def fixed_rate_map(fixed_rate):
    '''The telescope has several fixed slew rates you can invoke.
    Given a fixed rate index, return the corresponding rate in quarter arcseconds per second.'''
    siderial_rate = int(SIDERIAL_RATE_RADIANS_PER_SECOND / math.pi * 180 * 60 * 60 * 4)
    degree_per_second = 60 * 60 * 4
    if fixed_rate == 0:
        return 0
    if fixed_rate == 1:
        return int(0.5 * siderial_rate)
    if fixed_rate == 2:
        return 1 * siderial_rate
    if fixed_rate == 3:
        return 4 * siderial_rate
    if fixed_rate == 4:
        return 8 * siderial_rate
    if fixed_rate == 5:
        return 16 * siderial_rate
    if fixed_rate == 6:
        return 64 * siderial_rate
    if fixed_rate == 7:
        return 1 * degree_per_second
    if fixed_rate == 8:
        return 3 * degree_per_second
    if fixed_rate == 9:
        return 5 * degree_per_second
    raise Exception(f'Bad fixed rate: {fixed_rate}')

class NexStarError(Exception):
    '''Raised when the telescope does not respond, or gives an unexpected response.'''
    pass

BAUD_RATE = 9600

def speak_delay(speak_fun):
    '''Decorator used by NexStarSerialHootl to simulate communication delays with the telescope.'''
    def delayed_speak(self, command):
        time.sleep(0.04)
        response = speak_fun(self, command)
        time.sleep(0.05)
        return response
    return delayed_speak

class SerialNetClient(object):
    '''
    The telescope is connected to a different computer.
    Talk to it via an RPC server running on that computer.
    See telescope_server.py.
    '''
    def __init__(self, host_port):
        '''
        The argument is a string with the hostname or IP address of the RPC server,
        and the port number to connect to, separated by a colon. For example, '192.168.0.2:45345'.
        '''
        socket.setdefaulttimeout(5.0)
        self.client = rpc.RpcClient(host_port)
        assert self.client.call('hello') == 'hello'

    def speak(self, command):
        '''Send the telescope a command, and return its response (without the trailing '#').'''
        success, value = self.client.call('speak', command)

        if not success:
            raise NexStarError(repr(value))
        return value

    def close(self):
        pass

class NexStarSerialHootl(object):
    '''
    A telescope simulator used for Hardware Out Of The Loop (HOOTL) testing.
    This lets you test the software without the risk of damaging your telescope,
    and without the trouble of setting it up.

    The simulator runs in a separate thread.
    '''
    def __init__(self, current_time, observatory_location, altaz_mode):
        '''
        current_time should be an astropy.time.Time.

        observatory_location should be an astropy.coordinates.EarthLocation.

        altaz_mode should be True (indicating that the mount is vertical)
                   or False (indicating that it's on an equatorial wedge).
        '''
        self.altaz_mode = altaz_mode

        # Simulator state variables
        # We assume the telescope is perfectly aligned.
        self.state_azm_or_ra = 0 # 24 bit integer 
        self.state_alt_or_dec = 0 # 24 bit integer 

        self.state_location = observatory_location
        self.state_time = int(current_time.to_value('gps') * 1e9) # Integer nanoseconds since gps epoch.
        self.state_timestep = int(0.10 * 1e9) # Integer nanoseconds to advance per simulation step.

        self.tracked_sky_coord = None

        # Interface variables, shared between main and simulator thread.
        self.iface_meas_azm = 0 # 24 bit integer
        self.iface_meas_alt = 0 # 24 bit integer

        self.iface_meas_ra  = 0 # 24 bit integer
        self.iface_meas_dec = 0 # 24 bit integer

        self.iface_cmd_goto_azm = 0 # 24 bit integer
        self.iface_cmd_goto_alt = 0 # 24 bit integer

        self.iface_cmd_goto_ra  = 0 # 24 bit integer
        self.iface_cmd_goto_dec = 0 # 24 bit integer

        self.iface_goto_in_progress = False
        self.iface_goto_azm_alt = True

        self.iface_tracking_mode = TrackingMode.OFF

        self.iface_cmd_slew_rate_azm = 0 # integer number of quarter-arcseconds per second
        self.iface_cmd_slew_rate_alt = 0 # integer number of quarter-arcseconds per second

        # Mutex to lock the self.iface_* variables.
        self.iface_lock = threading.Lock()

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
            wall_time += self.state_timestep
            sleep_time = wall_time - int(time.time()*1e9)
            if sleep_time > 0:
                time.sleep(sleep_time/1e9)

            with self.iface_lock:
                # Advance time
                self.state_time += self.state_timestep
                current_time = astropy.time.Time(self.state_time / 1e9, format='gps')

                # Get an astropy.coordinates.AltAz and astropy.coordinates.SkyCoord
                # corresponding to the telescope's current position.
                if self.altaz_mode:
                    alt_az = coords.AltAz(
                        obstime=current_time,
                        location=self.state_location,
                        az=util.wrap_rad(b24_to_rad(self.state_azm_or_ra), -math.pi) * units.rad,
                        alt=util.clamp(util.wrap_rad(b24_to_rad(self.state_alt_or_dec), -math.pi), -math.pi/2, math.pi/2) * units.rad)
                    sky_coord = alt_az.transform_to(coords.SkyCoord(ra=0*units.rad, dec=0*units.rad))
                else:
                    sky_coord = coords.SkyCoord(ra=b24_to_rad(self.state_azm_or_ra)*units.rad, dec=b24_to_rad(self.state_alt_or_dec)*units.rad)
                    alt_az = sky_coord.transform_to(coords.AltAz(obstime=current_time, location=self.state_location))

                # Move the telescope, if necessary.
                next_tracked_sky_coord = None
                if self.iface_goto_in_progress:
                    # If we're executing a GOTO,

                    # determine the maximum possible speed.
                    max_movement = rad_to_b24(quarterarcseconds_to_rad(fixed_rate_map(9) * (self.state_timestep/1e9)))

                    if self.altaz_mode:
                        # determine the azimuth and elevation of the desired RA and Dec, if necessary,
                        if not self.iface_goto_azm_alt:
                            dest_sky_coord = coords.SkyCoord(ra=b24_to_rad(self.iface_cmd_goto_ra) * units.rad,
                                                             dec=b24_to_rad(self.iface_cmd_goto_dec) * units.rad)
                            dest_alt_az = dest_sky_coord.transform_to(alt_az)
                            self.iface_cmd_goto_azm = rad_to_b24(dest_alt_az.az.to(units.rad).value)
                            self.iface_cmd_goto_alt = rad_to_b24(dest_alt_az.alt.to(units.rad).value)

                        # and move to that position at the maximum possible speed.
                        self.state_azm_or_ra += util.clamp(self.iface_cmd_goto_azm-self.state_azm_or_ra, -max_movement, max_movement)
                        self.state_alt_or_dec += util.clamp(self.iface_cmd_goto_alt-self.state_alt_or_dec, -max_movement, max_movement)

                        # If we have completed the GOTO, note that.
                        if self.state_azm_or_ra == self.iface_cmd_goto_azm and self.state_alt_or_dec == self.iface_cmd_goto_alt:
                            self.iface_goto_in_progress = False
                    else:
                        # determine the RA and Dec of the desired altitude and elevation, if necessary,
                        if self.iface_goto_azm_alt:
                            dest_alt_az = coords.AltAz(
                                obstime=current_time,
                                location=self.state_location,
                                az=util.wrap_rad(b24_to_rad(self.iface_cmd_goto_azm), -math.pi) * units.rad,
                                alt=util.clamp(util.wrap_rad(b24_to_rad(self.iface_cmd_goto_alt), -math.pi), -math.pi/2, math.pi/2) * units.rad)
                            dest_sky_coord = alt_az.transform_to(coords.SkyCoord(ra=0*units.rad, dec=0*units.rad))

                            self.iface_cmd_goto_ra = rad_to_b24(dest_alt_az.az.to(units.rad).value)
                            self.iface_cmd_goto_dec = rad_to_b24(dest_alt_az.alt.to(units.rad).value)

                        # and move to that position at the maximum possible speed.
                        self.state_azm_or_ra += util.clamp(self.iface_cmd_goto_ra-self.state_azm_or_ra, -max_movement, max_movement)
                        self.state_alt_or_dec += util.clamp(self.iface_cmd_goto_dec-self.state_alt_or_dec, -max_movement, max_movement)

                        # If we have completed the GOTO, note that.
                        if self.state_azm_or_ra == self.iface_cmd_goto_ra and self.state_alt_or_dec == self.iface_cmd_goto_dec:
                            self.iface_goto_in_progress = False
                elif self.iface_tracking_mode != TrackingMode.OFF:
                    # If we're tracking,
                    assert self.iface_cmd_slew_rate_azm == 0
                    assert self.iface_cmd_slew_rate_alt == 0

                    if self.altaz_mode:
                        # note the current position of the telescope if we don't already have a tracked position saved,
                        if self.tracked_sky_coord is None:
                            self.tracked_sky_coord = sky_coord

                        # and then just snap the telescope to that position. The motion should be small, so it's fine.
                        tracked_alt_az = self.tracked_sky_coord.transform_to(alt_az)
                        self.state_azm_or_ra = rad_to_b24(tracked_alt_az.az.to(units.rad).value)
                        self.state_alt_or_dec = rad_to_b24(tracked_alt_az.alt.to(units.rad).value)
                        next_tracked_sky_coord = self.tracked_sky_coord
                    else:
                        # On an equatorial wedge, the telescope is motionless relative to the sky when tracking.
                        pass
                else:
                    # If we're slewing, slew.

                    if self.altaz_mode:
                        siderial_rate_correction = 0.0
                    else:
                        # When the telescope is stopped, the right ascension naturally drifts at the sidereal rate.
                        siderial_rate_correction = SIDERIAL_RATE_RADIANS_PER_SECOND

                    self.state_azm_or_ra += int(wrap_b24(rad_to_b24(quarterarcseconds_to_rad(self.iface_cmd_slew_rate_azm) + siderial_rate_correction), -2**23) * (self.state_timestep/1e9))
                    self.state_alt_or_dec += int(wrap_b24(rad_to_b24(quarterarcseconds_to_rad(self.iface_cmd_slew_rate_alt)), -2**23) * (self.state_timestep/1e9))
                self.tracked_sky_coord = next_tracked_sky_coord

                # Get an astropy.coordinates.AltAz and astropy.coordinates.SkyCoord
                # corresponding to the telescope's current position.
                if self.altaz_mode:
                    alt_az = coords.AltAz(
                        obstime=current_time,
                        location=self.state_location,
                        az=util.wrap_rad(b24_to_rad(self.state_azm_or_ra), -math.pi) * units.rad,
                        alt=util.clamp(util.wrap_rad(b24_to_rad(self.state_alt_or_dec), -math.pi), -math.pi/2, math.pi/2) * units.rad)
                    sky_coord = alt_az.transform_to(coords.SkyCoord(ra=0*units.rad, dec=0*units.rad))
                else:
                    sky_coord = coords.SkyCoord(ra=b24_to_rad(self.state_azm_or_ra)*units.rad, dec=b24_to_rad(self.state_alt_or_dec)*units.rad)
                    alt_az = sky_coord.transform_to(coords.AltAz(obstime=current_time, location=self.state_location))

                # Update the position measurements.
                if self.altaz_mode:
                    self.iface_meas_azm = self.state_azm_or_ra
                    self.iface_meas_alt = self.state_alt_or_dec

                    self.iface_meas_ra  = rad_to_b24(sky_coord.ra.to(units.rad).value)
                    self.iface_meas_dec = rad_to_b24(sky_coord.dec.to(units.rad).value)
                else:
                    self.iface_meas_azm = rad_to_b24(alt_az.az.to(units.rad).value)  
                    self.iface_meas_alt = rad_to_b24(alt_az.alt.to(units.rad).value) 

                    self.iface_meas_ra  = self.state_azm_or_ra  
                    self.iface_meas_dec = self.state_alt_or_dec 

    @speak_delay
    def speak(self, command):
        '''Decode and execute a command, then encode and return a response.'''
        # If the simulator thread died, just give up.
        if not self.thread.is_alive():
            sys.exit(1)

        with self.iface_lock:
            assert len(command) > 0

            def match_passthrough(p1, p2, p3, nargs):
                '''
                Return True if this command is a passthrough command with the
                given prefix ID numbers and number of arguments.
                '''
                if len(command) != 8:
                    return False

                prefix_matches = (command[0] == 'P' and
                                  command[1] == chr(p1) and
                                  command[2] == chr(p2) and
                                  command[3] == chr(p3))
                if not prefix_matches:
                    return False

                for arg in [4, 5, 6, 7]:
                    if arg-4 >= nargs:
                        if command[arg] != chr(0):
                            return False

                return True

            # Get RA/DEC
            if command == 'E':
                return '{},{}'.format(b24_to_hex4(self.iface_meas_ra), b24_to_hex4(self.iface_meas_dec))

            # Get precise RA/DEC
            if command == 'e':
                return '{},{}'.format(b24_to_hex8(self.iface_meas_ra), b24_to_hex8(self.iface_meas_dec))

            # Get AZM-ALT
            if command == 'Z':
                if not self.altaz_mode:
                    raise Exception('The real mount does not return accurate results for GET AZM-ALT when in EQ mode')
                return '{},{}'.format(b24_to_hex4(self.iface_meas_azm), b24_to_hex4(self.iface_meas_alt))

            # Get precise AZM-ALT
            if command == 'z':
                if not self.altaz_mode:
                    raise Exception('The real mount does not return accurate results for GET AZM-ALT when in EQ mode')
                return '{},{}'.format(b24_to_hex8(self.iface_meas_azm), b24_to_hex8(self.iface_meas_alt))

            # GOTO RA/DEC
            if command[0] == 'R':
                assert len(command) == 10
                assert command[5] == ','
                self.iface_cmd_goto_ra = from_hex(command[1:5]) << 8
                self.iface_cmd_goto_dec = from_hex(command[6:10]) << 8
                self.iface_goto_in_progress = True
                self.iface_goto_azm_alt = False
                return ''

            # GOTO precise RA/DEC
            if command[0] == 'r':
                assert len(command) == 18
                assert command[9] == ','
                self.iface_cmd_goto_ra = from_hex(command[1:9]) >> 8
                self.iface_cmd_goto_dec = from_hex(command[10:18]) >> 8
                self.iface_goto_in_progress = True
                self.iface_goto_azm_alt = False
                return ''

            # GOTO AZM-ALT
            if command[0] == 'B':
                assert len(command) == 10
                assert command[5] == ','
                self.iface_cmd_goto_azm = from_hex(command[1:5]) << 8
                self.iface_cmd_goto_alt = from_hex(command[6:10]) << 8
                self.iface_goto_in_progress = True
                self.iface_goto_azm_alt = True
                return ''

            # GOTO precise AZM-ALT
            if command[0] == 'b':
                assert len(command) == 18
                assert command[9] == ','
                self.iface_cmd_goto_azm = from_hex(command[1:9]) >> 8
                self.iface_cmd_goto_alt = from_hex(command[10:18]) >> 8
                self.iface_goto_in_progress = True
                self.iface_goto_azm_alt = True
                return ''

            # Get Tracking Mode
            if command == 't':
                return chr(self.iface_tracking_mode.value)

            # Set Tracking Mode
            if command[0] == 'T':
                assert len(command) == 2
                self.iface_tracking_mode = TrackingMode(ord(command[1]))
                return ''

            # Variable rate Azm slew in positive direction (or RA slew in negative direction)
            if match_passthrough(3, 16, 6, 2):
                slew_rate_hi = ord(command[4])
                slew_rate_lo = ord(command[5])
                self.iface_cmd_slew_rate_azm = slew_rate_hi * 256 + slew_rate_lo
                if not self.altaz_mode:
                    self.iface_cmd_slew_rate_azm = -self.iface_cmd_slew_rate_azm
                return ''

            # Variable rate Azm slew in negative direction (or RA slew in positive direction)
            if match_passthrough(3, 16, 7, 2):
                slew_rate_hi = ord(command[4])
                slew_rate_lo = ord(command[5])
                self.iface_cmd_slew_rate_azm = -1 * slew_rate_hi * 256 + slew_rate_lo
                if not self.altaz_mode:
                    self.iface_cmd_slew_rate_azm = -self.iface_cmd_slew_rate_azm
                return ''

            # Variable rate Alt (or Dec) slew in positive direction
            if match_passthrough(3, 17, 6, 2):
                slew_rate_hi = ord(command[4])
                slew_rate_lo = ord(command[5])
                self.iface_cmd_slew_rate_alt = slew_rate_hi * 256 + slew_rate_lo
                return ''

            # Variable rate Alt (or Dec) slew in negative direction
            if match_passthrough(3, 17, 7, 2):
                slew_rate_hi = ord(command[4])
                slew_rate_lo = ord(command[5])
                self.iface_cmd_slew_rate_alt = -1 * slew_rate_hi * 256 + slew_rate_lo
                return ''

            # Fixed rate Azm slew in positive direction (or RA slew in negative direction)
            if match_passthrough(3, 16, 36, 1):
                self.iface_cmd_slew_rate_azm = fixed_rate_map(ord(command[4]))
                return ''

            # Fixed rate Azm slew in negative direction (or RA slew in positive direction)
            if match_passthrough(3, 16, 37, 1):
                self.iface_cmd_slew_rate_azm = -1 * fixed_rate_map(ord(command[4]))
                return ''

            # Fixed rate Alt (or Dec) slew in positive direction
            if match_passthrough(3, 17, 36, 1):
                self.iface_cmd_slew_rate_alt = fixed_rate_map(ord(command[4]))
                return ''

            # Fixed rate Alt (or Dec) slew in negative direction
            if match_passthrough(3, 17, 37, 1):
                self.iface_cmd_slew_rate_alt = -1 * fixed_rate_map(ord(command[4]))
                return ''

            # Echo
            if command[0] == 'K':
                assert len(command) == 2
                return command[1]

            # Is GOTO in Progress?
            if command == 'L':
                return ('1' if self.iface_goto_in_progress else '0')

            # Cancel GOTO
            if command == 'M':
                self.iface_goto_in_progress = False
                return ''

            raise Exception('Invalid or unimplemented command: "{}"'.format(repr(command)))

class NexStar(object):
    '''The main interface for speaking to a NexStar telescope.

    Call member functions to send commands with arguments in sensible units,
    and they will return replies in sensible units.'''
    def __init__(self, serial_port):
        '''
        The argument is an object that provides a speak() function for talking to the
        telescope in the NexStar serial communication protocol. Can be either of
        SerialNetClient or NexStarSerialHootl.
        '''
        self.serial_port = serial_port

    def _speak(self, command, response_len):
        '''Helper function that calls self.serial_port.speak() and validates the response length.'''
        response = self.serial_port.speak(command)
        if len(response) != response_len:
            raise NexStarError(repr(response))
        return response

    def get_ra_dec(self):
        '''Return current Right Ascension and Declination of telescope in radians, with low precision.'''
        r = self._speak('E', 9)
        assert r[4] == ','
        ra = b24_to_rad(from_hex(r[0:4]) << 8)
        dec = b24_to_rad(from_hex(r[5:9]) << 8)
        return ra, dec

    def get_precise_ra_dec(self):
        '''Return current Right Ascension and Declination of telescope in radians, with high precision.'''
        r = self._speak('e', 17)
        assert r[8] == ','
        ra = b24_to_rad(from_hex(r[0:8]) >> 8)
        dec = b24_to_rad(from_hex(r[9:17]) >> 8)
        return ra, dec

    def get_azm_alt(self):
        '''Return current azimuth and elevation of telescope in radians, with low precision.'''
        r = self._speak('Z', 9)
        assert r[4] == ','
        azm = b24_to_rad(from_hex(r[0:4]) << 8)
        alt = b24_to_rad(from_hex(r[5:9]) << 8)
        return azm, alt

    def get_precise_azm_alt(self):
        '''Return current azimuth and elevation of telescope in radians, with high precision.'''
        r = self._speak('z', 17)
        assert r[8] == ','
        azm = b24_to_rad(from_hex(r[0:8]) >> 8)
        alt = b24_to_rad(from_hex(r[9:17]) >> 8)
        return azm, alt

    def goto_ra_dec(self, ra, dec):
        '''GOTO the specified Right Ascension and Declination, with low precision.'''
        command = 'R{},{}'.format(b24_to_hex4(rad_to_b24(ra)), b24_to_hex4(rad_to_b24(dec)))
        self._speak(command, 0)

    def goto_precise_ra_dec(self, ra, dec):
        '''GOTO the specified Right Ascension and Declination, with high precision.'''
        command = 'r{},{}'.format(b24_to_hex8(rad_to_b24(ra)), b24_to_hex8(rad_to_b24(dec)))
        self._speak(command, 0)

    def goto_azm_alt(self, azm, alt):
        '''GOTO the specified azimuth and elevation, with low precision.'''
        command = 'B{},{}'.format(b24_to_hex4(rad_to_b24(azm)), b24_to_hex4(rad_to_b24(alt)))
        self._speak(command, 0)

    def goto_precise_azm_alt(self, azm, alt):
        '''GOTO the specified azimuth and elevation, with high precision.'''
        command = 'b{},{}'.format(b24_to_hex8(rad_to_b24(azm)), b24_to_hex8(rad_to_b24(alt)))
        self._speak(command, 0)

    def get_tracking_mode(self):
        '''Get the current TrackingMode of the telescope.'''
        return TrackingMode(ord(self._speak('t', 1)))

    def set_tracking_mode(self, mode):
        '''Set the current TrackingMode of the telescope.'''
        self._speak('T{}'.format(chr(mode.value)), 0)

    def slew_azm_or_ra(self, rate):
        '''
        Set the azimuth/RA slew rate of the telescope, in radians per second.

        RA slew is backwards.
        '''
        arg = rad_to_quarterarcseconds(min(abs(rate), 0.079121))
        arg = min(arg, 0xffff)
        arg_hi = chr(int(arg / 256))
        arg_lo = chr(int(arg % 256))
        dir_arg = chr(6) if rate >= 0 else chr(7)
        self._speak('P' + chr(3) + chr(16) + dir_arg + arg_hi + arg_lo + chr(0) + chr(0), 0)

    def slew_alt_or_dec(self, rate):
        '''Set the elevation/declination slew rate of the telescope, in radians per second.'''
        arg = rad_to_quarterarcseconds(min(abs(rate), 0.079121))
        arg = min(arg, 0xffff)
        arg_hi = chr(int(arg / 256))
        arg_lo = chr(int(arg % 256))
        dir_arg = chr(6) if rate >= 0 else chr(7)
        self._speak('P' + chr(3) + chr(17) + dir_arg + arg_hi + arg_lo + chr(0) + chr(0), 0)

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

    def is_goto_in_progress(self):
        '''Return True if a GOTO is in progress.'''
        response = self._speak('L', 1)
        assert response in '01'
        return response == '1'

    def get_focus_position(self):
        '''Get the position of the focus motor. Units are unclear.'''
        r = self._speak('P' + chr(1) + chr(18) + chr(1) + chr(0) + chr(0) + chr(0) + chr(3), 3)
        return ord(r[0])*256*256 + ord(r[1])*256 + ord(r[2])

    def goto_focus(self, focus_position):
        '''Tell the focus motor to go to a specific position. Units are unclear.'''
        arg_hi = chr(int(focus_position / 256 / 256))
        arg_md = chr(int(focus_position / 256 % 256))
        arg_lo = chr(int(focus_position % 256))
        self._speak('P' + chr(4) + chr(18) + chr(2) + arg_hi + arg_md + arg_lo + chr(0), 0)

    def goto_focus_dist(self, distance):
        '''
        Tell the focus motor to set the focal length of the telescope to a certain distance, in meters.

        This requires a set of empirically determined calibration coefficients that vary
        depending on exactly what lenses and cameras you've put on the back of the telescope.
        Currently the calibration is just hardcoded, so this function is not very useful.
        It's just a proof of concept.
        '''
        # Erect image diagnonal, 25mm eyepiece
        #slope = 201136.36480722183
        #offset = -291260.05810772214

        # focal reducer, Erect image diagnonal, 25mm eyepiece
        slope = 151644.14905741333
        offset = -206465.29184105826

        self.goto_focus(slope * math.atan(distance) + offset)

    def get_focus_limits(self):
        '''Return the minimum and maximum focus positions. Units are unclear.'''
        r = self._speak('P' + chr(1) + chr(18) + chr(44) + chr(0) + chr(0) + chr(0) + chr(8), 8)
        lo = ord(r[0])
        lo = ord(r[1]) + lo * 256
        lo = ord(r[2]) + lo * 256
        lo = ord(r[3]) + lo * 256
        hi = ord(r[4])
        hi = ord(r[5]) + hi * 256
        hi = ord(r[6]) + hi * 256
        hi = ord(r[7]) + hi * 256
        return (lo, hi)
