#!/usr/bin/env python3

'''Read and dump the complete NexPlane configuration, for debugging purposes.'''

import yaml
import sys

import config

def parse_args_and_config():
    '''Parse the configuration data and command line arguments consumed by this script.'''
    parser, config_data = config.get_arg_parser_and_config_data(
        description='Read and dump the complete NexPlane configuration, for debugging purposes.')

    return parser.parse_args(), config_data

def main():
    args, config_data = parse_args_and_config()
    yaml.dump(config_data, sys.stdout)

if __name__ == '__main__':
    main()
