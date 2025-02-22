#!/usr/bin/env python

'''Run a SkyWatcherUdpServerHootl for testing purposes.'''

import sys

import skywatcher

def main() -> None:
    server = skywatcher.SkyWatcherUdpServerHootl(int(sys.argv[1]))
    server.run()

if __name__ == '__main__':
    main()
