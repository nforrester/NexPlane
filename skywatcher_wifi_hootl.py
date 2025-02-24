#!/usr/bin/env python

'''Run a SkyWatcherUdpServerHootl for testing purposes.'''

import sys

import skywatcher

def main() -> None:
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        port = 11880
    server = skywatcher.SkyWatcherUdpServerHootl(int(sys.argv[1]))
    server.run()

if __name__ == '__main__':
    main()
