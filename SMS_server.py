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
import sys
import time
import os.path
import argparse
import logging
import asyncio

# Application
import utils
import daemons
from utils import CLIENT_ID

DIRECTORY = os.path.dirname(os.path.realpath(__file__))

DEFAULT_LOGLEVEL = 'INFO'
DEFAULT_CONFIGNAME = 'verite.conf'
SPECFILE_EXT = '.spec'
DEFAULT_CONFIGFILE = os.path.join(DIRECTORY, DEFAULT_CONFIGNAME)



OSC_VRAI = utils.CustomOscMessage('/vrai')
OSC_FAUX = utils.CustomOscMessage('/faux')
OSC_RESET = utils.CustomOscMessage('/reponses', [0, 0])
OSC_FINSONDAGE = utils.CustomOscMessage('/finsondage', [])


logger = logging.getLogger('SMS Server')



class Server(daemons.PickleStreamProtocol):
    def __init__(self, loop, config):
        self._config = config
        self._globals = config['globals']
        self._regex = config['regex']

        # SMS storage structures
        self._timeline = []
        self._messages = []

        secret = self._globals['secret'].encode('utf8')
        secret_pattern = re.escape(secret) + self._regex['port_pattern'].encode('utf8')
        inbox = config['server']['inbox']

        # Listening daemons
        self._listener = daemons.UDPBroadcastListener(loop=loop, secret_pattern=secret_pattern, 
                                                    callback=self.got_secret, 
                                                    endianness=self._globals['endianness'], 
                                                    )
        self._scanner = daemons.BroadcastServer(loop=loop, protocol=self._listener, **self._globals)
        self._watcher = daemons.SMSWatcher(directory=inbox, callback=self.got_sms)

        # Transports
        self._display_transport = None

        # Temporary task for TCP connection
        self._connecting = None
        super().__init__(loop, **self._globals)
        
        # Temporary poll & video tasks
        self._poll = None
        self._poll_running = False
        
    @asyncio.coroutine
    def setup(self):
        display_conf = self._config['display']
        display_peer = (display_conf['addr'], display_conf['port'])
        self._display_transport, _ = yield from self._loop.create_datagram_endpoint(
                                                        asyncio.DatagramProtocol, 
                                                        remote_addr=display_peer)
        yield from self._watcher.setup(self._loop)
        self._scanner.start()
        
    @asyncio.coroutine
    def stop(self):
        yield from self._watcher.stop()
        yield from self._scanner.stop()
        if self._peer_transport:
            self._peer_transport.close()
        self._display_transport.close()
        
    def got_sms(self, sms_path):
        filename = os.path.basename(sms_path)
        metadata = re.match(self._regex['sms_pattern'], filename, re.I)
        if metadata is None:
            logger.debug('Invalid SMS: %r', filename)
            return

        try:
            content = open(sms_path).read()
#            TODO: add phone to phonebook anyway ?
        except UnicodeDecodeError:
            logger.debug('Wrong encoding in SMS: %r', filename)
            return
            
        if content == 'Delivered' or re.match('.*ce correspondant a cherché à vous joindre.*', content):
            logger.debug('Skipping Automatic Network SMS: %r', filename)
            return
        
        content = content.strip().replace('\n', ' ')
        sms_data = metadata.groupdict()
        sms_data['phone'] = '0' + sms_data['phone']
        vote_match = re.match(self._regex['vote_pattern'], content, re.I)
        
        if vote_match:
            vote = bool(vote_match.group('vrai'))
            logger.debug('Got {} vote: "{}" from {}'.format(vote, content, filename))
            sms_data['type'] = utils.EventTypes.vrai if vote else utils.EventTypes.faux
            sms_data['data'] = 'VRAI' if vote else 'FAUX'
            if self._poll_running:
                self.vote(vote)
        # TODO: handle multipart messages (see python-gammu)
        else: # message
            logger.debug('Got Message in SMS: %r -> %r', filename, content)
            self._add_message(sms_data['phone'], content)
            sms_data['type'] = utils.EventTypes.message
            sms_data['data'] = content
        
        event = utils.Event(**sms_data)
        self._add_event(event)
        
    def got_secret(self, port, peer):
        if not self._connecting:
            tcp_connection = self._loop.create_connection(lambda: self, host=peer[0], port=port)
            self._connecting = self._loop.create_task(tcp_connection)
            self._connecting.add_done_callback(self._connecting_done)
        
    def _connecting_done(self, task):
        if task.exception():
            logger.error('TCP Connection error: %r', task.exception())
        self._connecting = None
        
    @asyncio.coroutine
    def rig_poll(self, choice, times):
        for _ in range(times):
            self.vote(choice)
            yield from asyncio.sleep(0.1)

    def vote(self, choice):
        message = OSC_VRAI if choice else OSC_FAUX
        self.send_to_display(message)
        
    def _add_message(self, phone, content, to_display=False, displayed=False):
        index = len(self._messages)
        message = [phone, content, to_display, displayed]
        self._messages.append(message)
        if self._peer_transport:
            self._loop.call_soon(self.transmit_message, index, message)
        return index
        
    def _add_event(self, event):
        utils.insort_record(self._timeline, event)
        if self._peer_transport:
            # Theoretical max size of a multipart SMS payload = 153 chars * 255 parts = 39015 bytes
            self._loop.call_soon(self.transmit_event, event)
        
    def transmit_event(self, event):
        self.request_peer('/event', event)
        
    def transmit_message(self, index, message):
        self.request_peer('/message', [index] + list(message))
        
    def send_to_display(self, message):
        logger.debug('Sending Osc over UDP to display %r', message)
        self._display_transport.sendto(message.dgram)
                            
    ###########################################################################
    # Osc processing callbacks
    ###########################################################################
    def process_delete(self, payload, timestamp):
        index = payload[0]
        if index < len(self._messages):
            self._messages[index][1:4] = ['', False, False]
            logger.debug('DELETE messsage with id: %r', index)
        else:
            logger.error('Message index out of range in OSC erase command: %d', index)
            
    def process_newmessage(self, payload, timestamp):
        content = payload[0]
        index = self._add_message(CLIENT_ID, content, to_display=True, displayed=False)
        sms = utils.Event(timestamp=timestamp, phone=CLIENT_ID, data=content, type=utils.EventTypes.message)
        self._add_event(sms)
        logger.debug('NEW Message - id: %r, content: %r', index, content)
        
    def process_to_display(self, payload, timestamp):
        id, to_display = payload
        self._messages[id][2] = to_display

    def process_messages(self, payload, timestamp):
        displayed = '        '.join(self._messages[id][1] for id in payload)
        msg = utils.CustomOscMessage('/messages', displayed or ' ')
        self.send_to_display(msg)

        for id, msg in enumerate(self._messages):
            msg[-1] = (id in payload)

        logger.debug('DISPLAYING Messages: %r', payload)
                
    def process_sondage(self, payload, timestamp):
        titre, chrono = payload

        if chrono <= 0:
            chrono = -2
        else:
            _format = self._globals['timestamp_format']
            seconds = time.mktime(time.strptime(timestamp, _format))
            end_timestamp = time.strftime(_format, time.localtime(seconds + chrono))
            self._poll = self._loop.call_later(chrono, self._record_finsondage, end_timestamp)
            logger.debug('Scheduling FIN Sondage after %r seconds', chrono)

        self.send_to_display(OSC_RESET)
        self.send_to_display(utils.CustomOscMessage('/sondage', [titre, chrono]))

        type = utils.EventTypes.sondage
        event = utils.Event(timestamp=timestamp, phone=CLIENT_ID, data=titre, type=type)
        self._add_event(event)

        self._poll_running = True
        logger.debug('SONDAGE {}, chrono:{}'.format(*payload))
        
    def process_finsondage(self, payload, timestamp):
        self.send_to_display(OSC_FINSONDAGE)
        self._record_finsondage(timestamp)
        
    def _record_finsondage(self, timestamp):
        if self._poll:
            self._poll.cancel()
            self._poll = None

        type = utils.EventTypes.fin_sondage
        event = utils.Event(timestamp=timestamp, phone=CLIENT_ID, data='FIN SONDAGE', type=type)
        self._add_event(event)

        self._poll_running = False
        logger.debug('FIN SONDAGE')
        
    ###########################################################################
    # asyncio.Protocol API
    ###########################################################################
    def connection_made(self, transport):
        super().connection_made(transport)
        for index, record in enumerate(self._messages):
            if record[1]: # skip erased or empty messages
                self._loop.call_soon(self.transmit_message, index, record)
        for event in self._timeline:
            self._loop.call_soon(self.transmit_event, event)
        

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SMS Server for the show #Vérité')
    parser.add_argument('-l', '--loglevel', type=str, choices=list(logging._nameToLevel), default=DEFAULT_LOGLEVEL)
    parser.add_argument('-c', '--configfile', type=open, default=DEFAULT_CONFIGFILE)
    parser.add_argument('-s', '--specfile', type=open)
    args = parser.parse_args()
    # TODO: send logging over TCP to client
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

    with Server(loop, config) as server:
        loop.run_forever()
