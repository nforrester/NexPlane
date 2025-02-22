'''The get_arg_parser_and_config_data() function is the star of the show here.'''

import argparse
import copy
import os
import yaml

from typing import Any

def main_config_dir() -> str:
    '''
    Return the directory containing this file, which is where the config_default.yaml and
    config.yaml should be.
    '''
    return os.path.dirname(os.path.abspath(__file__))

def merge_config(under: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    '''Merge two nested dictionaries, picking the value from the second whenever they conflict.'''
    output = copy.deepcopy(under)
    for key, value in over.items():
        if key in output and isinstance(output[key], dict) and isinstance(value, dict):
            output[key] = merge_config(output[key], value)
        else:
            output[key] = copy.deepcopy(value)
    return output

def read_config(extra_configs: list[str]) -> dict[str, Any]:
    '''
    Read config_default.yaml and config.yaml, with the second taking priority in case of conflict.
    Then read additional config files specified in extra_configs, with each taking priority over
    the last.
    '''
    with open(os.path.join(main_config_dir(), 'config_default.yaml')) as f:
        data = yaml.load(f.read(), yaml.Loader)

    try:
        with open(os.path.join(main_config_dir(), 'config.yaml')) as f:
            data = merge_config(data, yaml.load(f.read(), yaml.Loader))
    except FileNotFoundError:
        pass

    for filename in extra_configs:
        with open(filename) as f:
            data = merge_config(data, yaml.load(f.read(), yaml.Loader))

    return data

def get_arg_parser_and_config_data(*args: Any, **kwargs: Any) -> tuple[argparse.ArgumentParser, dict[str, Any]]:
    '''
    Parse the --config options from the command line, read the config files,
    then return an ArgumentParser constructed with the supplied args and kwargs
    which has the --config option already added. Also return the config data.
    This allows you to alter the defaults for other command line options
    depending on the contents of the files specified by the --config options.
    '''
    def add_config_arg(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            '--config', action='append', type=str, default=[],
            help='Specify an additional config file to be read that takes priority over '
                 'config.yaml and config_default.yaml. This option can be provided multiple '
                 'times, causing multiple config files to be read, with later ones taking '
                 'priority over earlier ones. These config files may alter the defaults for '
                 'other command line arguments, in effect serving as customized short-hand '
                 'for complex argument combinations.')

    config_arg_parser = argparse.ArgumentParser(add_help=False)
    add_config_arg(config_arg_parser)
    config_args, _ = config_arg_parser.parse_known_args()
    config_data = read_config(config_args.config)

    arg_parser = argparse.ArgumentParser(*args, **kwargs)
    add_config_arg(arg_parser)
    return arg_parser, config_data
