'''Base functionality used by manufacturer-specific interface code.'''

import enum
import rpc
import time

from abc import ABC, abstractmethod
from typing import TypeVar, Callable

class CommError(Exception):
    '''Raised when the telescope does not respond, or gives an unexpected response.'''
    pass

class Client(ABC):
    '''Talk to the telescope mount.'''

    @abstractmethod
    def speak(self, command: str) -> str:
        '''Send the mount a command, and return its response.'''

    @abstractmethod
    def close(self) -> None:
        '''Close the connection to the telescope mount.'''

class TrackingMode(enum.Enum):
    '''Tracking modes the telescope can use. This is NexStar-specific.'''
    OFF      = 0
    ALT_AZ   = 1
    # Do not use equatorial tracking modes
    #EQ_NORTH = 2
    #EQ_SOUTH = 3

class Mount(ABC):
    '''The main interface for speaking to a telescope mount.

    Call member functions to send commands with arguments in sensible units,
    and they will return replies in sensible units,
    typically radians or radians per second.'''

    @abstractmethod
    def get_ra_dec(self) -> tuple[float, float]:
        '''Return (RA, Dec).

        May not be properly aligned.
        May not be valid if the telescope is in altaz mode.'''

    @abstractmethod
    def get_azm_alt(self) -> tuple[float, float]:
        '''Return (Azm, Alt). Might not be properly aligned.

        May not be properly aligned.
        May not be valid if the telescope is in eq mode.'''

    @abstractmethod
    def slew_azm_or_ra(self, rate: float) -> None:
        '''Slew the Azimuth/RA axis at the specified rate.'''

    @abstractmethod
    def slew_alt_or_dec(self, rate: float) -> None:
        '''Slew the Altitude/Declination axis at the specified rate.'''

    @abstractmethod
    def slew_azm(self, rate: float) -> None:
        '''Slew the Azimuth axis at the specified rate.'''

    @abstractmethod
    def slew_alt(self, rate: float) -> None:
        '''Slew the Altitude axis at the specified rate.'''

    @abstractmethod
    def slew_ra(self, rate: float) -> None:
        '''Slew the RA axis at the specified rate.'''

    @abstractmethod
    def slew_dec(self, rate: float) -> None:
        '''Slew the Declination axis at the specified rate.'''

    @abstractmethod
    def slew_azmalt(self, azm_rate: float, alt_rate: float) -> None:
        '''Set the Az/Alt slew rates.'''

    @abstractmethod
    def slew_radec(self, ra_rate: float, dec_rate: float) -> None:
        '''Set the RA/Dec slew rates.'''

    @abstractmethod
    def set_tracking_mode(self, mode: TrackingMode) -> None:
        '''Set tracking mode, if applicable.'''

class SerialNetClient(Client):
    '''
    The telescope is connected to a different computer.
    Talk to it via an RPC server running on that computer.
    See telescope_server.py.
    '''
    def __init__(self, host_port: str):
        '''
        The argument is a string with the hostname or IP address of the RPC server,
        and the port number to connect to, separated by a colon. For example, '192.168.0.2:45345'.
        '''
        self.client = rpc.RpcClient(host_port)
        assert self.client.call('hello') == 'hello'

    def speak(self, command: str) -> str:
        '''Send the telescope a command, and return its response (without the trailing '#').'''
        success, value = self.client.call('speak', command)

        if not success:
            raise CommError(repr(value))

        assert isinstance(value, str)
        return value

    def close(self) -> None:
        pass

T = TypeVar('T')

def speak_delay(speak_fun: Callable[[T, str], str]) -> Callable[[T, str], str]:
    '''Decorator used by HOOTL to simulate communication delays with the telescope.'''
    def delayed_speak(self: T, command: str) -> str:
        time.sleep(0.04)
        response = speak_fun(self, command)
        time.sleep(0.05)
        return response
    return delayed_speak

