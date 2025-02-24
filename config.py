'''The get_arg_parser_and_config_data() function is the star of the show here.'''

import argparse
import copy
import os
import yaml

from util import VALID_MOUNT_MODES, VALID_TELESCOPE_PROTOCOLS

from typing import Any, Callable

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

    assert isinstance(data, dict)
    return data

ArgValidators = list[Callable[[argparse.Namespace], None]]

def get_arg_parser_and_config_data(*args: Any, **kwargs: Any) -> tuple[argparse.ArgumentParser, dict[str, Any], ArgValidators]:
    '''
    Parse the --config options from the command line, read the config files,
    then return an ArgumentParser constructed with the supplied args and kwargs
    which has the --config option already added. Also return the config data.
    This allows you to alter the defaults for other command line options
    depending on the contents of the files specified by the --config options.
    '''
    def add_arg_config(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            '--config', action='append', type=str, default=[],
            help='Specify an additional config file to be read that takes priority over '
                 'config.yaml and config_default.yaml. This option can be provided multiple '
                 'times, causing multiple config files to be read, with later ones taking '
                 'priority over earlier ones. These config files may alter the defaults for '
                 'other command line arguments, in effect serving as customized short-hand '
                 'for complex argument combinations.')

    config_arg_parser = argparse.ArgumentParser(add_help=False)
    add_arg_config(config_arg_parser)
    config_args, _ = config_arg_parser.parse_known_args()
    config_data = read_config(config_args.config)

    arg_parser = argparse.ArgumentParser(*args, **kwargs)
    add_arg_config(arg_parser)

    validators: ArgValidators = []

    return arg_parser, config_data, validators

def add_arg_alignment(parser: argparse.ArgumentParser, config_data: dict[str, Any], validators: ArgValidators) -> None:
    '''Add --alignment to parser.'''
    default = config_data['alignment']

    parser.add_argument(
        '--alignment', type=str, default=default,
        help='File storing telescope alignment data, '
             'created by align.py and read by nexplane.py. '
             '(default: ' + (default if default else 'None') + ')')

def add_arg_hootl(parser: argparse.ArgumentParser, config_data: dict[str, Any], validators: ArgValidators) -> None:
    '''Add --hootl and --no-hootl to parser.'''
    parser.add_argument(
        '--hootl', dest='run_hootl', action='store_true',
        help='Do not connect to a real telescope, but instead run an '
             'internal simulation of the telescope. This is useful for testing.' +
             (' This is the default' if config_data['hootl'] else ''))
    parser.add_argument(
        '--no-hootl', dest='run_hootl', action='store_false',
        help='Opposite of --hootl.' +
             (' This is the default' if not config_data['hootl'] else ''))
    parser.set_defaults(run_hootl=config_data['hootl'])

def add_arg_location(parser: argparse.ArgumentParser, config_data: dict[str, Any], validators: ArgValidators) -> None:
    '''Add --location to parser.'''
    parser.add_argument(
        '--location', type=str, default=config_data['location'],
        help='Where are you? Pick a named location from your config file '
             '(default: ' + config_data['location'] + ')')

    def v(args: argparse.Namespace) -> None:
        if args.location not in config_data['locations']:
            raise Exception('Error, invalid --location' + repr(args.location) +
                            '. Valid values are: ' + (', '.join(config_data['locations'].keys())))
    validators.append(v)

def add_arg_landmark(parser: argparse.ArgumentParser, config_data: dict[str, Any], validators: ArgValidators) -> None:
    '''Add --landmark to parser.'''
    default = config_data['landmark']

    parser.add_argument(
        '--landmark', type=str, default=config_data['landmark'],
        help='If it is not possible to use the telescope\'s internal alignment '
             'functions (perhaps because it is cloudy), you can manually point '
             'the telescope at a location listed in your config file, and then '
             'start this program with the --landmark option specifying where '
             'the telescope is pointed. The offset between the known location '
             'and the telescope\'s reported position will be recorded and '
             'compensated for. '
             '(default: ' + (default if default else 'None') + ')')

    def v(args: argparse.Namespace) -> None:
        if not args.landmark:
            return
        if args.landmark not in config_data['locations']:
            raise Exception('Error, invalid --landmark ' + repr(args.landmark) +
                            '. Valid values are: ' + (', '.join(config_data['locations'].keys())))
    validators.append(v)

def add_arg_telescope(parser: argparse.ArgumentParser, config_data: dict[str, Any], validators: ArgValidators) -> None:
    '''Add --telescope to parser.'''
    parser.add_argument(
        '--telescope', type=str, default=config_data['telescope_server'],
        help='The host:port of the telescope_server.py process, which talks '
             'to the telescope mount '
             '(default: ' + config_data['telescope_server'] + ')')

    def v(args: argparse.Namespace) -> None:
        if ':' not in args.telescope:
            raise Exception('Error, invalid --telescope' + repr(args.telescope) +
                            '. Should be: <host>:<port>')
    validators.append(v)

def add_arg_telescope_protocol(parser: argparse.ArgumentParser, config_data: dict[str, Any], validators: ArgValidators) -> None:
    '''Add --telescope-protocol to parser.'''
    parser.add_argument(
        '--telescope-protocol', type=str, default=config_data['telescope_protocol'],
        help='Which protocol to use to talk to the telescope (default: {})'.format(config_data['telescope_protocol']))

    def v(args: argparse.Namespace) -> None:
        if args.telescope_protocol not in VALID_TELESCOPE_PROTOCOLS:
            raise Exception('Error, invalid --telescope-protocol ' + repr(args.telescope_protocol) +
                            '. Valid values are: ' + (', '.join(VALID_TELESCOPE_PROTOCOLS)))
    validators.append(v)

def add_arg_mount_mode(parser: argparse.ArgumentParser, config_data: dict[str, Any], validators: ArgValidators) -> None:
    '''Add --mount-mode to parser.'''
    parser.add_argument(
        '--mount-mode', type=str, default=config_data['mount_mode'],
        help='Type of telescope mount, either altaz or eq. Default: {}'.format(
            config_data['mount_mode']))

    def v(args: argparse.Namespace) -> None:
        if args.mount_mode not in VALID_MOUNT_MODES:
            raise Exception('Error, invalid --mount-mode ' + repr(args.mount_mode) +
                            '. Valid values are: ' + (', '.join(VALID_MOUNT_MODES)))
    validators.append(v)

def validate(validators: ArgValidators, args: argparse.Namespace) -> None:
    '''Validate arguments.'''
    for validator in validators:
        validator(args)
