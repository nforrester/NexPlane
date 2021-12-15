#!/usr/bin/env python3

'''
Run this on the computer with the telescope attached.
It assumes that there's only one USB serial device, and that that's the telescope.
It starts an RPC server on UDP port 45345 and listens for commands from the main application.
This lets you run the main application on a different computer from the telescope connection, if you wish.
I find this convenient because my laptop doesn't have the right drivers, but my raspberry pi does.

The RPC server provides one important command: speak().
It takes one argument, a line of text to send to the telescope.
It returns (bool, string), whether the telescope sent a valid-looking response, and what the response was
(minus the trailing '#' character).
'''

import ast
import os
import serial
import sys
import time

import config
import rpc

BAUD_RATE = 9600

def read_response(telescope):
    response = ''
    start = time.monotonic()
    while start + 3.5 > time.monotonic() and '#' not in response:
        response += telescope.read().decode(encoding='ISO-8859-1')
    return response

def hello():
    return 'hello'

def nexstar_serial_udp_server(serial_port, net_port):
    print('Opening', serial_port)
    sys.stdout.flush()
    telescope = serial.Serial(port=serial_port, baudrate=BAUD_RATE, timeout=0)

    def speak(line):
        telescope.write(line.encode(encoding='ISO-8859-1'))

        response = read_response(telescope)
        if len(response) == 0 or response[-1] != '#':
            return (False, response)
        else:
            return (True, response[:-1])

    print('Starting RPC server...')
    sys.stdout.flush()
    server = rpc.RpcServer(net_port)
    server.add_fun(hello)
    server.add_fun(speak)
    print('Ready.')
    sys.stdout.flush()
    server.run()

def parse_args_and_config():
    '''Parse the configuration data and command line arguments consumed by this script.'''
    parser, config_data = config.get_arg_parser_and_config_data(
        description='Exposes the NexStar serial interface on the network.')

    parser.add_argument(
        '--serial-port', default=config_data['serial_port'],
        help='Which serial port to use (default: ' +
             ('the first port it finds between /dev/ttyUSB0 and /dev/ttyUSB9.'
              if config_data['serial_port'] == 'auto' else config_data['serial_port']) + ')')

    parser.add_argument(
        '--network-port', type=int, default=45345,
        help='Which network port to use (default: 45345)')

    return parser.parse_args(), config_data

def main():
    args, config_data = parse_args_and_config()

    serial_port = args.serial_port
    if serial_port == 'auto':
        serial_port = None
        for i in range(10):
            this_port = f'/dev/ttyUSB{i}'
            if os.path.exists(this_port):
                serial_port = this_port
                break
        if serial_port is None:
            print('Unable to find serial port for telescope.')
            sys.stdout.flush()
            sys.exit(1)
    nexstar_serial_udp_server(serial_port, args.network_port)

if __name__ == '__main__':
    main()
