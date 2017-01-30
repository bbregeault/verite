#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# 
# Copyright (c) 2017 Benoit Bregeault

# ---------------------------------------------------------------------

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


# Standard library
import re
import os.path
import bisect
from enum import IntEnum
from collections import namedtuple, Iterable

# Third Party
from pythonosc.osc_message_builder import OscMessageBuilder
from pythonosc.osc_message import OscMessage
from configobj import ConfigObj, ConfigObjError
from validate import VdtTypeError, VdtValueError, Validator


CLIENT_ID = '     CLIENT     '

Event = namedtuple('Event', ('timestamp', 'phone', 'data', 'type'))
EventTypes = IntEnum('EventTypes', 'vrai faux message sondage fin_sondage')
https_re = r'https://.*\..*/.*\?'


def validate_https_url(value):
    if not isinstance(value, str):
        raise VdtTypeError(value)
    elif not re.fullmatch(https_re, value):
        raise VdtValueError(value)
    return value
    
def validate_directory(value):
    if not isinstance(value, str):
        raise VdtTypeError(value)
    if not os.path.isdir:
        raise VdtValueError(value)
    return value
    
def validate_re(value):
    if not isinstance(value, str):
        raise VdtTypeError(value)
    try:
        re.compile(value)
    except Exception:
        raise VdtValueError(value)
    return value
    
check_functions = { 'https_url': validate_https_url, 
                    'directory': validate_directory,
                    're': validate_re, 
                  }
custom_validator = Validator(check_functions)
    
def parse_configfile(configfile, configspec):
    config = ConfigObj(configfile, configspec=configspec, interpolation=False, encoding='utf8')
    passed = config.validate(custom_validator)
    if passed is not True:
        sections = [k for k, v in passed.items() if v is not True]
        raise ConfigObjError('Error in config file sections {}'.format(sections))
    return config

def insort_record(sorted_list, record):
    if not sorted_list or record >= sorted_list[-1]:
        index = len(sorted_list)
        sorted_list.append(record)
    else:
        index = bisect.bisect(sorted_list, record)
        sorted_list.insert(index, record)
    return index

def forge_secret(secret, port, endianness='big'):
    return secret.encode('utf8') + port.to_bytes(2, endianness)

def find_max_udp_payload(host='127.0.0.1', port=9999):
    limits = [0, 65536]
    msg = b'A'
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while limits[1] - limits[0] > 1:
        half = round(sum(limits) / 2)
        try:
            sock.sendto(msg * half, (host, port))
            limits[0] = half
        except OSError as e:
            if e.errno != 90:
                raise
            limits[1] = half
    
    return limits[0]


class CustomOscMessage(OscMessage):
    def __init__(self, string, params=()):
        if isinstance(string, bytes):
            if params != ():
                raise TypeError('Params cannot be passed along with a bytes datagram')
            super().__init__(string)
        else:
            builder = OscMessageBuilder(address=string)
            if not isinstance(params, Iterable) or isinstance(params, (str, bytes)):
                params = [params]
            for item in params:
                builder.add_arg(item)
            super().__init__(builder.build().dgram)
            
    def __repr__(self):
        return "{}('{_address_regexp}', {_parameters})".format(self.__class__.__qualname__, **self.__dict__)
