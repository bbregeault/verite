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


DEFAULT_LOGLEVEL = 'INFO'
DEFAULT_CONFIGFILE = './verite.conf'
SPECFILE_EXT = '.spec'

# Standard library
import sys
import os.path
import argparse
import logging
import asyncio
from socket import AF_INET
from urllib.parse import urlencode
from functools import wraps

# Third Party
import aiohttp

# Application
import utils
import daemons


logger = logging.getLogger(__name__)

def threadsafe_method(func):
    @wraps(func)
    def wrapper(self, *args):
        self._loop.call_soon_threadsafe(func, self, *args)
    return wrapper

class Client(daemons.PickleStreamProtocol):
    def __init__(self, loop, config, callbacks):
        self._config = config
        self._globals = config['globals']
        self._sms_conf = config['sms_service']
        super().__init__(loop, **self._globals)
        
        self.connection_state_cb = None
        for name, target in callbacks.items():
            setattr(self, name, target)

        options = self._sms_conf['send_options'].copy()
        options.update(self._sms_conf['credentials'])
        self._base_sms_url = self._sms_conf['fastapi_url'] + urlencode(options, encoding='utf8') + '&'
        self._credits_url = self._sms_conf['credits_url'] + urlencode(self._sms_conf['credentials'], encoding='utf8')
        
    @asyncio.coroutine
    def setup(self):
        self._server = yield from self._loop.create_server(lambda: self, port=0, family=AF_INET)
        tcp_port = self._server.sockets[0].getsockname()[1]
        secret_message = utils.forge_secret(self._globals['secret'], tcp_port, self._globals['endianness'])
        self._scanner = daemons.BroadcastClient(loop=self._loop, secret_message=secret_message, tcp_port=tcp_port, **self._globals)
        self._scanner.start()
        logger.debug('SMS Client Initialization finished')
        
    @asyncio.coroutine
    def stop(self):
        yield from self._scanner.stop()
        self._server.close()
        yield from self._server.wait_closed()
        if self._peer_transport:
            logger.critical('Closing TCP transport: %r', self._peer_transport)
            self._peer_transport.close()
        logger.debug('SMS Client closed successfully, transport: %r', self._peer_transport)
        
    @threadsafe_method
    def request_server(self, request, params):
        self.request_peer(request, params)
        
    @threadsafe_method
    def send_sms(self, sender, content, phonelist, callback):
        t = self._loop.create_task(self.async_send_sms(sender, content, phonelist))
        t.add_done_callback(callback)
        
    @asyncio.coroutine
    def async_send_sms(self, sender, content, phonelist):
        base_url = self._base_sms_url + urlencode({'sender': sender, 'text': content}, encoding='utf8') + '&to='
        tasks = []
        
        with aiohttp.ClientSession() as session:
            for number in phonelist:
                task = asyncio.ensure_future(self.timeout_get(base_url + number, session))
                tasks.append(task)
                
            responses = yield from asyncio.gather(*tasks, loop=self._loop, return_exceptions=True)
            # gather returns results in the original input order
            return {p: r for p, r in zip(phonelist, responses)}
        
    @threadsafe_method
    def get_credits(self, callback):
        t = self._loop.create_task(self.async_get_credits())
        t.add_done_callback(callback)
        
    @asyncio.coroutine
    def async_get_credits(self):        
        with aiohttp.ClientSession() as session:
            return (yield from self.timeout_get(self._credits_url, session))
        
    @asyncio.coroutine
    def timeout_get(self, url, session):
        with aiohttp.Timeout(10):
            response = yield from session.get(url)
            return (yield from response.text())
            
    ###########################################################################
    # asyncio.Protocol API
    ###########################################################################
    def connection_made(self, transport):
        super().connection_made(transport)
        if self.connection_state_cb:
            self.connection_state_cb(state=True, peername=transport.get_extra_info('peername'))
        
    def connection_lost(self, exc):
        super().connection_lost(exc)
        if self.connection_state_cb:
            self.connection_state_cb(state=False, exc=exc)
            

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SMS Server for the show #Vérité')
    parser.add_argument('-l', '--loglevel', type=str, choices=list(logging._nameToLevel), default=DEFAULT_LOGLEVEL)
    parser.add_argument('-c', '--configfile', type=open, default=DEFAULT_CONFIGFILE)
    parser.add_argument('-s', '--specfile', type=open)
    args = parser.parse_args()
    logging.basicConfig(level=args.loglevel)
    
    configfile = args.configfile
    if args.specfile:
        specfile = args.specfile
    else:
        root, _ = os.path.splitext(configfile.name) 
        try:
            specfile = open(root + SPECFILE_EXT)
        except OSError:
            specfile = None
        
    try:
        config = utils.parse_configfile(configfile, configspec=specfile)
    except utils.ConfigObjError as e:
        logger.critical('Error in configuration file', exc_info=True)
        sys.exit(e)
    finally:
        configfile.close()
        if specfile:
            specfile.close()
        
    loop = asyncio.get_event_loop()
    loop.set_debug(True)

    with Client(loop, config) as client:
        loop.run_forever()
