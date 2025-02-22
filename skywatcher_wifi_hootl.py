#!/usr/bin/env python

# TODO DOC ME

import sys

import skywatcher

def main():
    server = skywatcher.SkyWatcherUdpServerHootl(int(sys.argv[1]))
    server.run()

if __name__ == '__main__':
    main()
