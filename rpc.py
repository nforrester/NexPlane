'''I couldn't find a low latency Remote Procedure Call (RPC) library on the internet (shocking, I know!), so I wrote one myself.

It sends commands and replies as UDP packets.
Each packet is sent in triplicate to protect against dropped packets.
If a response is not received promptly, the data is sent again
(though after trying for long enough it will time out).
Message and client IDs are employed to ensure that each command is executed at most once on the server,
even though the command may be received several times, and the reply may be sent several times.
'''

import ast
import copy
import heapq
import random
import socket
import select
import time
import traceback

class DupDetector:
    '''
    We need to detect duplicate messages so we can ignore them. Message IDs come
    mostly sequentially, but can arrive slightly out of order. We can't just
    keep track of the last message ID received and reject all IDs less than that
    because they might come out of order. We also can't just keep an ever-growing
    set of IDs we've seen because that would be a huge memory leak.

    To solve this we store the received message IDs in both a set and a heap.
    The set is for fast lookup of individual IDs. The heap is to keep track of
    the maximum ID for which we know that all lesser IDs have been observed.
    Upon receiving a new ID we can then start by checking the heap, then checking
    the set. When the lowest and second lowest IDs currently stored are adjacent
    we can then remove the lowest to keep memory usage from growing.
    '''
    def __init__(self):
        self.heap = [-1]            # Keep track of the maximum ID for which we know all lower IDs have been observed.
        self.set = set(self.heap)   # Keep track of IDs in the sparse region.

    def is_new(self, num):
        '''Return True if this is the first call to is_new() with this value as an argument.'''
        # If this ID is less than the minimum ID we're currently tracking, we've seen it.
        if num <= self.heap[0]:
            return False

        # If this ID is in the set of IDs we're currently tracking, we've seen it.
        if num in self.set:
            return False

        # This ID is new! Add it to the set and the heap.
        self.set.add(num)
        heapq.heappush(self.heap, num)

        # If the minimum ID and the next higher ID are both currently tracked, there's no
        # need to keep tracking the minimum ID, so stop tracking it.
        while (self.heap[0] + 1) in self.set:
            self.set.remove(self.heap[0])
            heapq.heappop(self.heap)

        return True

    def lowest_still_tracked(self):
        '''Return the maximum ID such that all lower IDs have been seen.'''
        return self.heap[0]

class RpcConnectionFailure(Exception):
    '''Raised on the client if the connection to the server fails.'''
    pass

class RpcRemoteException(Exception):
    '''Raised on the client if the command on the server raises an exception.'''
    pass

class RpcClient:
    '''Make Remote Procedure Calls to a server which executes them.'''
    def __init__(self, host_port):
        '''
        The argument is a string with the hostname or IP address of the RPC server,
        and the port number to connect to, separated by a colon. For example, '192.168.0.2:45345'.
        '''
        host, port = host_port.split(':')
        self.host_port = (host, int(port))

        # Listen for replies on port+1.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', int(port)+1))

        self.reset_connection()

    def reset_connection(self):
        '''
        Reset the connection to the server, because more packets
        have been dropped than can be compensated for.
        '''
        # ID of the next command.
        self.counter = 0

        # Track the IDs of received responses.
        self.response_tracker = DupDetector()

        # ID of this client. The probability of two clients picking the same ID is low.
        self.client_id = random.randint(0, 100000000000000)

    def call(self, fun, *args, **kwargs):
        '''
        Call a function on the server. If it returns a value, return it.
        If it raises an exception, raise an RpcRemoteException.
        '''
        # Encode the message to the server.
        message = repr((
            self.client_id,
            self.counter,
            self.response_tracker.lowest_still_tracked(),
            fun,
            args,
            kwargs)).encode()

        overall_timeout = 1.0 # How long to wait before giving up.
        salvo_timeout = 0.1   # How long to wait before retransmitting.
        salvo_size = 3        # How many duplicate packets to send in each salvo.
        try:
            give_up_time = time.monotonic() + overall_timeout
            while time.monotonic() < give_up_time:
                # Send a salvo of packets.
                for _ in range(salvo_size):
                    self.sock.sendto(message, self.host_port)
                # Await a reply, timing out at salvo_failure_time.
                salvo_failure_time = min(time.monotonic() + salvo_timeout, give_up_time)
                while time.monotonic() < salvo_failure_time:
                    ready, _, _ = select.select([self.sock], [], [], salvo_failure_time - time.monotonic())
                    # If we got a reply,
                    if ready:
                        # receive it,
                        data, _ = self.sock.recvfrom(10000)
                        # decode it,
                        (reply_counter, exception, value) = ast.literal_eval(data.decode())
                        # see if it's a reply to the message we sent,
                        if reply_counter == self.counter:
                            # note that we received it,
                            self.response_tracker.is_new(reply_counter)
                            # and either raise an exception or return a value, as appropriate.
                            if exception is not None:
                                raise RpcRemoteException(exception)
                            return value
        finally:
            # Increment the message ID.
            self.counter += 1

        self.reset_connection()
        raise RpcConnectionFailure('Connection failure.')

class RpcServer:
    '''Service Remote Procedure Calls and report the results back to the clients.'''
    def __init__(self, port):
        host_port = ('0.0.0.0', port)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(host_port)

        self.dups = dict()                  # For each client ID, stores a DupDetector.
        self.dup_responses = dict()         # For each client ID, stores a dictionary that remembers the response for each command
        self.dup_responses_horizon = dict() # The minimum key remaining in each element of dup_responses.
        self.funs = dict()                  # What functions do we implement?

        # This server should provide the get_funs function so clients can understand their options.
        self.add_fun_named('get_funs', self.get_funs)

    def add_fun_named(self, name, fun):
        '''Register a function that can be called by the clients, specifying the name of the function as an argument.'''
        self.funs[name] = fun

    def add_fun(self, fun):
        '''Register a function that can be called by the clients, picking the name of the function from its __name__ attribute.'''
        self.funs[fun.__name__] = fun

    def get_funs(self):
        '''Return the list of functions this server provides.'''
        return list(self.funs.keys())

    def run(self):
        '''Run the server.'''
        while True:
            # Wait for, receive, and parse a request.
            ready, _, _ = select.select([self.sock], [], [])
            assert ready
            data, addr_and_port = self.sock.recvfrom(10000)
            (client_id, counter, new_horizon, fun, args, kwargs) = ast.literal_eval(data.decode())

            # If this is the first message from a given client, make entries for it in dups and dup_responses.
            if client_id not in self.dups:
                self.dups[client_id] = DupDetector()
                self.dup_responses[client_id] = dict()

            if not self.dups[client_id].is_new(counter):
                # If this message is not new, respond with the stored response.
                self.sock.sendto(*(self.dup_responses[client_id][counter]))
                continue
            else:
                # If this message is new, execute the function, store the response, and send a salvo of 3 responses.
                try:
                    value = self.funs[fun](*args, **kwargs)
                    exception = None
                except KeyboardInterrupt:
                    raise
                except:
                    value = None
                    exception = traceback.format_exc()

                message = repr((counter, exception, value)).encode()
                response = (message, addr_and_port)
                self.dup_responses[client_id][counter] = response
                salvo_size = 3
                for _ in range(salvo_size):
                    self.sock.sendto(*response)

                # Clear out old responses that have been successfully transmitted from dup_responses.
                if client_id not in self.dup_responses_horizon:
                    self.dup_responses_horizon[client_id] = counter
                if self.dup_responses_horizon[client_id] < new_horizon:
                    for i in range(self.dup_responses_horizon[client_id], new_horizon):
                        del self.dup_responses[client_id][i]
                    self.dup_responses_horizon[client_id] = new_horizon
