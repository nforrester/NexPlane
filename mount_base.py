'''Base functionality used by manufacturer-specific interface code.'''

import rpc
import time

class CommError(Exception):
    '''Raised when the telescope does not respond, or gives an unexpected response.'''
    pass

class SerialNetClient:
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
        self.client = rpc.RpcClient(host_port)
        assert self.client.call('hello') == 'hello'

    def speak(self, command):
        '''Send the telescope a command, and return its response (without the trailing '#').'''
        success, value = self.client.call('speak', command)

        if not success:
            raise CommError(repr(value))
        return value

    def close(self):
        pass

def speak_delay(speak_fun):
    '''Decorator used by HOOTL to simulate communication delays with the telescope.'''
    def delayed_speak(self, command):
        time.sleep(0.04)
        response = speak_fun(self, command)
        time.sleep(0.05)
        return response
    return delayed_speak

