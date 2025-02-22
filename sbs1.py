'''
Receive and process data about airplane trajectories in SBS-1 (BaseStation) format.

The data format is documented here:
http://woodair.net/sbs/article/barebones42_socket_data.htm

Live data about aircraft near you can be generated from an ADS-B receiver and dump1090:
https://flightaware.com/adsb/piaware/build/
https://github.com/antirez/dump1090

Liveish data about satellites overhead can be generated in the same format by
satellites.py. The software will just assume that they are very fast, very high
altitude airplanes.

Though there are a number of supporting classes and functions, the star of the show
here is Sbs1Receiver. You just tell it where you are and what servers to receive data
from, and then call get_planes() to get a dict of Airplane objects, organized by their
Aircraft Mode S hexadecimal code (hex code). The Airplane objects store the last known
position of the aircraft, and a function that lets you extrapolate that position into
the future based on its last known velocity.
'''

import copy
import math
import numpy
import queue
import socket
import sys
import threading
import multiprocessing
import time

from typing import TypeVar, Any

import astropy.coordinates as coords
import astropy.units as units

import util
from util import unwrap, assert_float

# Airplane data is considered stale if it's older than 30 seconds.
DROP_TIME_NS = 30e9

METERS_PER_FOOT = 0.3048

T = TypeVar('T')

class TimestampedDatum[T]:
    '''
    Records a data point about an airplane, and the time at which that data point
    was received. If this data point is calculated from several others, then it
    records the range of receipt times of the underlying data.
    '''
    def __init__(self) -> None:
        self.value: T | None = None       # The data point.
        self.min_time_ns: int | None = None # Earliest timestamp of any component of this datum.
        self.max_time_ns: int | None = None # Latest timestamp of any component of this datum.

    def set_time(self, time_ns: int) -> None:
        '''Set the receipt time of this data point.'''
        self.min_time_ns = time_ns
        self.max_time_ns = time_ns

    def set_times_from(self, others: list[Any]) -> None:
        '''
        Set the receipt time range of this data point to encompass the time ranges of
        several others. This is useful when calculating derived or composite data.
        '''
        self.min_time_ns = min([unwrap(o.min_time_ns) for o in others])
        self.max_time_ns = max([unwrap(o.max_time_ns) for o in others])

    def __str__(self) -> str:
        return str(self.value)

class RawAirplane:
    '''
    Records raw data about an airplane as received over the network,
    and provides a function to determine whether the data set is complete.
    This is necessary because not all incoming messages contain all the relevant
    information about an airplane.
    '''
    def __init__(self) -> None:
        self.hex         : TimestampedDatum[str]   = TimestampedDatum()
        self.callsign    : TimestampedDatum[str]   = TimestampedDatum()
        self.altitude    : TimestampedDatum[float] = TimestampedDatum()
        self.groundspeed : TimestampedDatum[float] = TimestampedDatum()
        self.track       : TimestampedDatum[float] = TimestampedDatum()
        self.lat         : TimestampedDatum[float] = TimestampedDatum()
        self.lon         : TimestampedDatum[float] = TimestampedDatum()
        self.vrate       : TimestampedDatum[float] = TimestampedDatum()

        self.callsign.value = '?'
        self.callsign.set_time(0)

    def complete_data(self) -> bool:
        '''
        Return True if all the necessary data is present to attempt pointing
        a telescope at the airplane.
        '''
        return self.hex.value         is not None and \
               self.callsign.value    is not None and \
               self.altitude.value    is not None and \
               self.groundspeed.value is not None and \
               self.track.value       is not None and \
               self.lat.value         is not None and \
               self.lon.value         is not None and \
               self.vrate.value       is not None

class Airplane:
    '''
    This is a more processed representation of the data about an airplane,
    more suitable for pointing telescopes. It can be computed from a RawAirplane
    by compute_airplane() if the RawAirplane object's complete_data() function
    returned True.

    Most interestingly, it has the position and velocity of the airplane
    expressed in the North East Down (NED) frame of the observatory,
    and the azimuth, elevation, and range from the observatory.
    '''
    def __init__(self) -> None:
        self.hex      : TimestampedDatum[str]           = TimestampedDatum()
        self.callsign : TimestampedDatum[str]           = TimestampedDatum()
        self.pos_ned  : TimestampedDatum[numpy.ndarray] = TimestampedDatum() # meters        in the NED frame of the observatory
        self.vel_ned  : TimestampedDatum[numpy.ndarray] = TimestampedDatum() # meters/second in the NED frame of the observatory
        self.az       : TimestampedDatum[float]         = TimestampedDatum() # radians
        self.el       : TimestampedDatum[float]         = TimestampedDatum() # radians
        self.range    : TimestampedDatum[float]         = TimestampedDatum() # meters
        self.in_space : TimestampedDatum[bool]          = TimestampedDatum() # boolean, approximate

        # The timestamp of the latitude measurement (and in practice, the longitude measurement
        # because empirically these typically come together). It is useful to track this
        # separately from the max and min times of pos_ned, because those are also affected by
        # altitude, and when extrapolating an airplane's position, the latitude and longitude
        # typically change much faster than the altitude. As such, this is the most useful time
        # to begin extrapolating from.
        self.lat_time_ns: int | None = None

    def __str__(self) -> str:
        return '{} {} {:6.1f} {:6.1f} {:10.1f}'.format(
            self.hex,
            self.callsign,
            unwrap(self.az.value) / 2 / math.pi * 360,
            unwrap(self.el.value) / 2 / math.pi * 360,
            unwrap(self.range.value))

    def extrapolate(self, time_ns: int) -> 'Airplane':
        '''Extrapolate this airplane's state into the future, and return a new Airplane object.'''
        new = Airplane()

        # The hex code, callsign, in-space-ness, and velocity are assumed to be constant.
        new.hex      = self.hex
        new.callsign = self.callsign
        new.vel_ned  = self.vel_ned
        new.in_space = self.in_space

        # Set the extrapolated latitude time.
        new.lat_time_ns = time_ns

        # How far into the future are we extrapolating, in seconds?
        extrapolation_time = (time_ns - unwrap(self.lat_time_ns)) / 1e9

        # Extrapolate the position based on the velocity.
        new.pos_ned.value = unwrap(self.pos_ned.value) + unwrap(new.vel_ned.value) * extrapolation_time
        new.pos_ned.set_time(time_ns)

        # Compute azimuth, elevation, and range from observatory.
        new.az.set_times_from([new.pos_ned])
        new.el.set_times_from([new.pos_ned])
        new.range.set_times_from([new.pos_ned])
        new.az.value, new.el.value, new.range.value = util.ned_to_aer(new.pos_ned.value)

        return new

class Sbs1Receiver:
    '''
    Manages a collection of threads and processes that ingest SBS-1 data from airplane
    servers and turn it into a continuously updated dict of Airplane objects that can
    be accessed from the main thread by calling self.get_planes().
    '''
    def __init__(self, plane_servers: list[str], observatory: coords.EarthLocation):
        '''Start all the threads and processes that do the work.'''
        # For each server, start a new multiprocessing.Process to receive data from it.
        # Each process has an associated multiprocessing.Queue to emit data
        # (sock_to_compute_qs).
        sock_to_compute_qs = []
        self.socket_procs = []
        for plane_server in plane_servers:
            sock_to_compute_q: multiprocessing.Queue[RawAirplane] = multiprocessing.Queue()
            socket_proc = multiprocessing.Process(
                target=receive_data,
                args=(plane_server, sock_to_compute_q))
            socket_proc.start()
            self.socket_procs.append(socket_proc)
            sock_to_compute_qs.append(sock_to_compute_q)

        # The data from the per-server processes are all collected by another process
        # that does the necessary computations to filter the data and turn RawAirplane
        # objects into Airplane objects. The Airplane objects are emitted from another
        # queue (compute_to_main_q).
        compute_to_main_q: multiprocessing.Queue[Airplane] = multiprocessing.Queue()
        self.compute_proc = multiprocessing.Process(
            target=compute_airplanes,
            args=(observatory, sock_to_compute_qs, compute_to_main_q))
        self.compute_proc.start()

        # Finally, a thread in the main process dequeues data from compute_to_main_q
        # and updates a dictionary from hex codes to Airplane objects, self.airplanes,
        # access to which is controlled by self.lock. The main thread in the main
        # process can access this data via self.get_planes().
        self.lock = threading.Lock()
        self.airplanes: dict[str, Airplane] = dict()

        self.stop_threads = False
        def run_dequeue_thread() -> None:
            self.run_dequeue_thread(compute_to_main_q)
        self.dequeue_thread = threading.Thread(target=run_dequeue_thread)
        self.dequeue_thread.start()

        # The point of all this nonsense is to run the expensive airplane computations
        # on a separate core from the main thread, which must run in real time.

    def close(self) -> None:
        '''Stop all the processes and threads.'''
        self.stop_threads = True
        self.compute_proc.terminate()
        for proc in self.socket_procs:
            proc.terminate()
        self.dequeue_thread.join()

    def run_dequeue_thread(self, in_q: 'multiprocessing.Queue[Airplane]') -> None:
        '''Thread that dequeues the output of the compute process and updates self.airplanes.'''
        # We periodically sweep self.airplanes for stale data and delete it.
        # This is when the last sweep happened so we know when it's time for
        # the next one.
        last_sweep_time = time.monotonic_ns()

        while not self.stop_threads:
            took_action_this_cycle = False

            # Pop airplanes off the queue and update self.airplanes with them
            # until the queue is empty.
            try:
                while True:
                    new = in_q.get_nowait()
                    with self.lock:
                        self.airplanes[unwrap(new.hex.value)] = new
                    took_action_this_cycle = True
            except queue.Empty:
                pass

            # If it's been more than a second since the last sweep for stale
            # planes, do another sweep and delete any stale planes.
            if last_sweep_time + 1e9 < time.monotonic_ns():
                with self.lock:
                    for hex_code in list(self.airplanes.keys()):
                        if time.monotonic_ns() - unwrap(self.airplanes[hex_code].hex.max_time_ns) > DROP_TIME_NS:
                            print('Drop (main) ', self.airplanes[hex_code].callsign.value)
                            del self.airplanes[hex_code]
                last_sweep_time = time.monotonic_ns()
                took_action_this_cycle = True

            # If we didn't do anything this cycle, sleep for a bit so as not
            # to use up a whole CPU doing nothing.
            if not took_action_this_cycle:
                time.sleep(0.05)

    def get_planes(self) -> dict[str, Airplane]:
        '''Get a dict from hex codes to Airplane objects for Airplanes currently present.'''
        with self.lock:
            return copy.deepcopy(self.airplanes)

def receive_data(plane_server: str, out_q: 'multiprocessing.Queue[RawAirplane]') -> None:
    '''Process that receives SBS-1 data from a server and emits RawAirplane objects in a queue.'''
    # SBS-1 data is a sequence of comma separated values. Not all fields are present in
    # every message, but every message has the same number of commas, so by skipping
    # N-1 commas you can always find the Nth field.
    #
    # This dictionary encodes the locations and data types of the fields we care about.
    # The parser uses this information to directly update RawAirplane objects.
    message_format = {
                                    # Field  0: Message type             (MSG, STA, ID, AIR, SEL or CLK)
        'ttype':       (1, int),    # Field  1: Transmission Type        MSG sub types 1 to 8. Not used by other message types.
                                    # Field  2: Session ID               Database Session record number
                                    # Field  3: AircraftID               Database Aircraft record number
        'hex':         (4, str),    # Field  4: HexIdent                 Aircraft Mode S hexadecimal code
                                    # Field  5: FlightID                 Database Flight record number
                                    # Field  6: Date message generated   As it says
                                    # Field  7: Time message generated   As it says
                                    # Field  8: Date message logged      As it says
                                    # Field  9: Time message logged      As it says
        'callsign':    (10, str),   # Field 10: Callsign                 An eight digit flight ID - can be flight number or registration (or even nothing).
        'altitude':    (11, float), # Field 11: Altitude                 Mode C altitude. Height relative to 1013.2mb (Flight Level). Not height AMSL..
        'groundspeed': (12, float), # Field 12: GroundSpeed              Speed over ground (not indicated airspeed)
        'track':       (13, float), # Field 13: Track                    Track of aircraft (not heading). Derived from the velocity E/W and velocity N/S
        'lat':         (14, float), # Field 14: Latitude                 North and East positive. South and West negative.
        'lon':         (15, float), # Field 15: Longitude                North and East positive. South and West negative.
        'vrate':       (16, float), # Field 16: VerticalRate             64ft resolution
                                    # Field 17: Squawk                   Assigned Mode A squawk code.
                                    # Field 18: Alert (Squawk change)    Flag to indicate squawk has changed.
                                    # Field 19: Emergency                Flag to indicate emergency code has been set
                                    # Field 20: SPI (Ident)              Flag to indicate transponder Ident has been activated.
                                    # Field 21: IsOnGround               Flag to indicate ground squat switch is active
    }

    # Not all message types are interesting. These ones are. Others are ignored.
    interesting_ttypes = [
        1, # 1 ES Identification and Category DF17 BDS 0,8
           # 2 ES Surface Position Message DF17 BDS 0,6 Triggered by nose gear squat switch.
        3, # 3 ES Airborne Position Message DF17 BDS 0,5
        4, # 4 ES Airborne Velocity Message DF17 BDS 0,9
           # 5 Surveillance Alt Message DF4, DF20 Triggered by ground radar. Not CRC secured.  MSG,5 will only be output if  the aircraft has previously sent a MSG,1, 2, 3, 4 or 8 signal.
           # 6 Surveillance ID Message DF5, DF21 Triggered by ground radar. Not CRC secured.  MSG,6 will only be output if  the aircraft has previously sent a MSG,1, 2, 3, 4 or 8 signal.
           # 7 Air To Air Message DF16 Triggered from TCAS.  MSG,7 is now included in the SBS socket output.
           # 8 All Call Reply DF11 Broadcast but also triggered by ground radar
    ]

    # Connect to the server.
    sock = socket.socket()
    host, port = plane_server.split(':')
    try:
        sock.connect((host, int(port)))
    except ConnectionRefusedError:
        print('Connection refused to plane server', plane_server)
        return

    # Every airplane we see is recorded here. This is necessary because not
    # all incoming messages contain all the relevant information about an
    # airplane, so we have to build up a picture from multiple messages.
    # Keys are hex codes, values are RawAirplane objects.
    raw_airplanes = dict()

    # We are receiving a TCP stream, a continuous stream of text. Messages
    # within the stream are separated by newlines. The chunks we get are
    # of unpredictable sizes, and in principle they could end in the middle
    # of a message, with the rest of the message coming in the next chunk.
    # The fragment variable stores unprocessed fragments of the stream from
    # one loop iteration to the next.
    fragment = ''

    while True:
        # Wait until we receive a new chunk of text, and split it on newlines
        # to form a list of message fragments we received.
        while True:
            try:
                messages = sock.recv(100000).decode().split('\n')
                break
            except socket.timeout:
                pass

        # Note the time of receipt.
        rx_time_ns = time.monotonic_ns()

        # The first message fragment needs to be joined with the last fragment
        # from the previous loop iteration to form a complete message.
        assert len(messages) > 0
        messages[0] = fragment + messages[0]

        # The last message fragment needs to be stored until the next loop
        # iteration.
        fragment = messages[-1]
        messages = messages[:-1]

        # For each complete message we received on this loop iteration:
        for message in messages:
            # Split up the message into fields.
            fields = message.split(',')
            assert len(fields) == 22

            # Use message_format to decode the fields into a dictionary
            # that contains the received data in appropriate formats.
            this_data = dict()
            for attr, info in message_format.items():
                index, converter = info
                if fields[index] != '':
                    this_data[attr] = converter(fields[index])
            assert 'hex' in this_data
            assert 'ttype' in this_data

            # Skip boring messages, and then forget the ttype value.
            if this_data['ttype'] not in interesting_ttypes:
                continue
            del this_data['ttype']

            # Drop messages with bogus coordinates.
            if ('lat' in this_data and (this_data['lat'] > 90 or this_data['lat'] < -90)) or \
               ('lon' in this_data and (this_data['lon'] > 180 or this_data['lon'] < -180)):
                print('Invalid lat/lon:', this_data)
                continue

            # Update the RawAirplane object for this plane.
            if this_data['hex'] not in raw_airplanes:
                raw_airplanes[this_data['hex']] = RawAirplane()
            airplane = raw_airplanes[this_data['hex']]
            for attr, value in this_data.items():
                datum = getattr(airplane, attr)
                datum.set_time(rx_time_ns)
                datum.value = value

            # If we have a complete data set, send the RawAirplane to the compute process.
            if airplane.complete_data():
                out_q.put(airplane)

def compute_airplane(observatory: coords.EarthLocation, raw_plane: RawAirplane) -> Airplane:
    '''Turn a RawAirplane into an Airplane.'''
    assert raw_plane.complete_data()

    plane = Airplane()
    plane.hex = raw_plane.hex
    plane.callsign = raw_plane.callsign

    plane.lat_time_ns = raw_plane.lat.max_time_ns

    # The McDowell line is considered to be the edge of space.
    plane.in_space.set_times_from([raw_plane.altitude])
    plane.in_space.value = unwrap(raw_plane.altitude.value) * METERS_PER_FOOT > 80000

    # POSITION

    # Get the airplane's EarthLocation.
    position = coords.EarthLocation.from_geodetic(
        raw_plane.lon.value,
        raw_plane.lat.value,
        raw_plane.altitude.value * units.imperial.ft,
        'WGS84')

    # Compute the position of the plane in the NED frame of the observatory.
    plane.pos_ned.set_times_from([raw_plane.lon, raw_plane.lat, raw_plane.altitude])
    plane.pos_ned.value = util.ned_between_earth_locations(position, observatory)

    # VELOCITY

    # Get the azimuth along which the airplane is travelling, in radians.
    vel_az = unwrap(raw_plane.track.value) / 360 * 2 * math.pi

    # Compute the velocity of the airplane in the airplane's NED frame.
    vel_ned_of_plane = numpy.array([
            math.cos(vel_az) * (raw_plane.groundspeed.value * units.imperial.kn).to(units.m / units.s).value,
            math.sin(vel_az) * (raw_plane.groundspeed.value * units.imperial.kn).to(units.m / units.s).value,
            -1 * (raw_plane.vrate.value * (units.imperial.ft / units.min)).to(units.m / units.s).value,
        ])

    # Transform the velocity to the geocentric frame.
    n_unit_plane, e_unit_plane, d_unit_plane = util.ned_unit_vectors_at_earth_location(position)
    vel_gc = (vel_ned_of_plane[0] * n_unit_plane +
              vel_ned_of_plane[1] * e_unit_plane +
              vel_ned_of_plane[2] * d_unit_plane)

    # Transform the velocity to the observatory's NED frame.
    n_unit_obs, e_unit_obs, d_unit_obs = util.ned_unit_vectors_at_earth_location(observatory)
    plane.vel_ned.set_times_from([raw_plane.track, raw_plane.groundspeed, raw_plane.vrate])
    plane.vel_ned.value = numpy.array([
            numpy.dot(vel_gc, n_unit_obs),
            numpy.dot(vel_gc, e_unit_obs),
            numpy.dot(vel_gc, d_unit_obs),
        ])

    # AZIMUTH, ELEVATION, RANGE
    plane.az.set_times_from([plane.pos_ned])
    plane.el.set_times_from([plane.pos_ned])
    plane.range.set_times_from([plane.pos_ned])
    plane.az.value, plane.el.value, plane.range.value = util.ned_to_aer(unwrap(plane.pos_ned.value))

    return plane

def compute_airplanes(observatory: coords.EarthLocation, in_qs: list['multiprocessing.Queue[RawAirplane]'], out_q: 'multiprocessing.Queue[Airplane]') -> None:
    '''Process that filters RawAirplane data and produces Airplane objects.'''
    # Contains up to date Airplane objects for all planes we've seen.
    computed_airplanes: dict[str, Airplane] = dict()

    while True:
        # Dequeue all the RawAirplane objects that have incorporated a new message.
        planes_with_updates = dict()
        for q_idx, in_q in enumerate(in_qs):
            try:
                while True:
                    new_raw = in_q.get_nowait()
                    # Modify the unique hex identifiers to avoid collisions between different data sources.
                    new_raw.hex.value = hex(q_idx) + unwrap(new_raw.hex.value)
                    planes_with_updates[new_raw.hex.value] = new_raw
            except queue.Empty:
                pass

        # If no updates have arrived, sleep for a bit so as not to use up a whole CPU doing nothing.
        if len(planes_with_updates) == 0:
            time.sleep(0.05)
            continue

        # For every airplane with new data:
        for hex_code, raw_airplane in planes_with_updates.items():
            # If it's been a long time since we got a position update, don't bother processing new messages about this plane.
            if raw_airplane.lat.max_time_ns is not None and time.monotonic_ns() - raw_airplane.lat.max_time_ns > DROP_TIME_NS:
                print('Drop (new)  ', raw_airplane.callsign.value)
                continue

            # Some planes transmit zero altitude (which is not true or useful). Don't bother processing messages about them.
            if raw_airplane.altitude.value is not None and raw_airplane.altitude.value == 0:
                continue

            # Compute an Airplane from the RawAirplane.
            new_plane = compute_airplane(observatory, raw_airplane)

            if hex_code not in computed_airplanes:
                # If this is a new airplane, take the update.
                take_update = True
            else:
                old_plane = computed_airplanes[hex_code]
                if unwrap(new_plane.lat_time_ns) > unwrap(old_plane.lat_time_ns) + DROP_TIME_NS:
                    # If the old data for this plane is from a long time ago, take the update.
                    print('Drop (old)  ', new_plane.callsign.value)
                    take_update = True
                elif new_plane.lat_time_ns == old_plane.lat_time_ns:
                    # If this new data doesn't provide an updated position, take the update.
                    take_update = True
                else:
                    # Decoding position data from ADS-B transmissions is non-trivial, and dump1090 is not
                    # able to do it perfectly (indeed, perfection may be impossible). As a result, sometimes
                    # incoming messages will repeat a stale position value as though it is current.
                    # In order to filter out these stale values, we assume that airplanes are not maneuvering
                    # so hard that they have large velocity changes between updates. Under this assumption,
                    # we compare the delta between the old and new position against the projected delta
                    # given the airplane's average velocity and the elapsed time. If the new position has the
                    # plane moving more than half way along the expected vector from the old position, we take
                    # the new position. This way if the old position was stale and the new position is good,
                    # then the delta will be very large and we'll take the update, but if the old position was
                    # good and the new position is stale, the delta will be small or zero and we will ignore
                    # the new position.
                    extrapolation_time = (unwrap(new_plane.lat_time_ns) - unwrap(old_plane.lat_time_ns)) / 1e9
                    avg_vel_ned = unwrap(new_plane.vel_ned.value)/2.0 + unwrap(old_plane.vel_ned.value)/2.0
                    delta_pos_ned_new = unwrap(new_plane.pos_ned.value) - unwrap(old_plane.pos_ned.value)
                    delta_pos_ned_old = extrapolation_time * avg_vel_ned
                    norm_new = assert_float(numpy.linalg.norm(delta_pos_ned_new))
                    norm_old = assert_float(numpy.linalg.norm(delta_pos_ned_old))
                    take_update = norm_new > norm_old * 0.5
                    if not take_update:
                        print('Drop (pos)  ', new_plane.callsign.value)

            # If we want to take the update, send the Airplane object to the main thread and update computed_airplanes.
            if take_update:
                out_q.put(new_plane)
                computed_airplanes[hex_code] = new_plane
