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
import os.path
import glob
import time
import pickle
import logging
import asyncio
from re import fullmatch

# Third Party
import aionotify
import netifaces

# Application
from utils import CustomOscMessage

logger = logging.getLogger(__name__)


class UDPBroadcastListener(asyncio.DatagramProtocol):
    def __init__(self, loop, secret_pattern, callback=None, endianness='big'):
        self._loop = loop
        self._secret_pattern = secret_pattern
        self._callback = callback
        self._endianness = endianness

    def datagram_received(self, data, peer):
        secret_match = fullmatch(self._secret_pattern, data)
        if secret_match:
            port = secret_match.group('port')
            port = int.from_bytes(port, self._endianness)
            logger.info('UDP Got Secret message with port %d from %r', port, peer)
            if self._callback is not None:
                self._loop.call_soon(self._callback, port, peer)
        else:
            logger.debug('UDP Wrong secret received: %r from %r', data, peer)


class BroadcastsScanner:
    def __init__(self, loop, udp_port, interfaces, *args, **kwargs):
        self._loop = loop
        self._interfaces = interfaces
        self._server_port = udp_port
        self._transports = {}
        self._waiter = asyncio.Future(loop=loop)
        self._running = asyncio.Event(loop=loop)
        self._task = None
        
    def start(self):
        self._task = self._loop.create_task(self.scan_available_broadcasts())
        self._task.add_done_callback(self._task_done)
        self._running.set()
        
    def _task_done(self, task):
        self._waiter.set_result(None)
        if not task.cancelled() and task.exception():
            logger.error('Error in Scanner task %r', task.exception())
        
    @asyncio.coroutine
    def stop(self):
        self._task.cancel()
        yield from self._waiter
        self._close_transports()
        
    def _close_transports(self, addr_set=None):
        if addr_set is None:
            addr_set = list(self._transports)
        for addr in addr_set:
            transport = self._transports.pop(addr)
            transport.close()
            logger.info('Closing UDP socket %r', transport.get_extra_info('sockname'))
        
    def pause(self):
        self._running.clear()
        
    def resume(self):
        self._running.set()

    @asyncio.coroutine
    def scan_available_broadcasts(self):
        while True:
            if not self._running.is_set():
                self._close_transports()
                yield from self._running.wait()
            bcast_addresses = set()
            for iface in self._interfaces:
                if iface not in netifaces.interfaces():
                    logger.warning('%s not in %r', iface, netifaces.interfaces())
                    continue
                addresses = netifaces.ifaddresses(iface)
                bcast_addresses.update([dic['broadcast'] for dic in addresses.get(netifaces.AF_INET, [])])
#            logger.debug('Sending addresses for setup: %r', bcast_addresses)
            yield from self._setup_broadcasts(bcast_addresses)
            yield from asyncio.sleep(1)
        
    @asyncio.coroutine
    def _setup_broadcasts(self, addresses):
        raise NotImplementedError
        
        
class BroadcastServer(BroadcastsScanner):
    def __init__(self, *args, protocol, **kwargs):
        super().__init__(*args, **kwargs)
        self._protocol = protocol
        
    def _get_protocol(self):
        return self._protocol
        
    @asyncio.coroutine
    def _setup_broadcasts(self, addresses):
        running = set(self._transports)
        if addresses != running:
            for addr in addresses - running:
                transport, _ = yield from self._loop.create_datagram_endpoint(self._get_protocol, local_addr=(addr, self._server_port))
                self._transports[addr] = transport
                logger.info('Added server listening to %r', transport.get_extra_info('sockname'))
            self._close_transports(running - addresses)


class BroadcastClient(BroadcastsScanner):
    def __init__(self, *args, secret_message, tcp_port, **kwargs):
        super().__init__(*args, **kwargs)
        self._secret_message = secret_message
        self._tcp_port = tcp_port
        self._connection = asyncio.async(self._loop.create_datagram_endpoint(
                                                        asyncio.DatagramProtocol, 
                                                        family=netifaces.AF_INET, 
                                                        allow_broadcast=True)
                                        )
        
    @asyncio.coroutine
    def _setup_broadcasts(self, addresses):
        if 'client' not in self._transports:
            logger.debug('Creating transport for secret broadcast')
            self._transports['client'], _ = yield from self._loop.create_datagram_endpoint(
                                                                asyncio.DatagramProtocol, 
                                                                family=netifaces.AF_INET, 
                                                                allow_broadcast=True)
        transport = self._transports['client']
        for addr in addresses:
            logger.debug('Send secret with port %d to %r, transport is %r, %r', self._tcp_port, (addr, self._server_port), id(transport), transport.is_closing())
            transport.sendto(self._secret_message, (addr, self._server_port))


class TCPPacketProtocol(asyncio.Protocol):
    def __init__(self, loop, tcp_header_size, endianness, timestamp_format, *args, **kwargs):
        self._peer_transport = None
        self._packet_len = 0
        self._buffer = b''
        self._loop = loop
        self._tcp_header_size = tcp_header_size
        self._endianness = endianness
        self._timestamp_format = timestamp_format
        logger.info('%s initialized successfully', self.__class__.__name__)
        
    def __enter__(self):
        self._loop.run_until_complete(self.setup())
        return self
        
    def __exit__(self, type, value, traceback):
        self._loop.run_until_complete(self.stop())
        self._loop.close()
        return isinstance(value, KeyboardInterrupt)
        
    def setup(self):
        raise NotImplementedError
        
    def stop(self):
        raise NotImplementedError
        
    def connection_made(self, transport):
        self._peer_transport = transport
        peername = transport.get_extra_info('peername')
        logger.info('TCP Connection established with %r', peername)
        self._scanner.pause()
        
    def connection_lost(self, exc):
        self._peer_transport.close()
        self._peer_transport = None
        logger.info("TCP Socket closed with error %r", exc)
        self._scanner.resume()
        
    def data_received(self, data):
        while data:
            if self._packet_len == 0:
                c = self._tcp_header_size - len(self._buffer)
                length, data = data[:c], data[c:]
                self._buffer += length
                if len(self._buffer) == self._tcp_header_size:
                    self._packet_len = int.from_bytes(self._buffer, self._endianness)
                    self._buffer = b''
#                    logger.debug('Got header : size={} bytes'.format(self._packet_len))
                    
            if self._packet_len > 0:
                c = self._packet_len - len(self._buffer)
                dgram, data = data[:c], data[c:]
                self._buffer += dgram
                if len(self._buffer) == self._packet_len:
                    timestamp = time.strftime(self._timestamp_format)
                    self._loop.call_soon(self._handle_packet, self._buffer, timestamp)
#                    logger.debug('Got OSC datagram size=%r bytes : %r', self._packet_len, self._buffer)
                    self._packet_len = 0
                    self._buffer = b''
                    
    def _handle_packet(self, packet, timestamp):
        raise NotImplementedError        
            
    def send_to_peer(self, *packets):
        if not all(isinstance(p, bytes) for p in packets):
            raise TypeError('All packets must be bytes instances')
        if not self._peer_transport:
            logger.error('Attempting to send packets to TCP client without a connection !')
            return
            
        for packet in packets:
            header = len(packet).to_bytes(self._tcp_header_size, self._endianness)
            logger.debug('Sending packet over TCP to client: %r', packet)
            self._peer_transport.write(header + packet)

class OSCStreamProtocol(TCPPacketProtocol):
    def _handle_packet(self, dgram, timestamp):
        try:
            message = CustomOscMessage(dgram)
        except Exception as e:
            logger.error('Invalid OSC datagram received : %r', e)
            return
        handler_name = 'process_' + message.address.lstrip('/')
        handler = getattr(self, handler_name, None)
        if handler:
            self._loop.call_soon(handler, message.params, timestamp)
        else:
            logger.error('Unknown handler for OSC address %r', message.address)
            
    def send_to_peer(self, *messages):
        super().send_to_peer(*(m.dgram for m in messages))
        
    def request_peer(self, command, params):
        msg = CustomOscMessage(command, params)
        super().send_to_peer(msg.dgram)


class PickleStreamProtocol(TCPPacketProtocol):
    def _handle_packet(self, packet, timestamp):
        try:
            command, payload = pickle.loads(packet)
        except ValueError:
            logger.error('Invalid Packet received: %r', pickle.loads(packet))
            return
        handler_name = 'process_' + command.lstrip('/')
        handler = getattr(self, handler_name, None)
        if handler:
            self._loop.call_soon(handler, payload, timestamp)
        else:
            logger.error('Unknown handler for Packet type %r', command)
            
    def send_to_peer(self, *packets):
        super().send_to_peer(*(pickle.dumps(p) for p in packets))
        
    def request_peer(self, command, params):
        packet = pickle.dumps([command, params])
        super().send_to_peer(packet)


class SMSWatcher(aionotify.Watcher):
    def __init__(self, directory, callback):
        super().__init__()
        self.directory = directory
        self._callback = callback
        self._task = None
        self._waiter = asyncio.Future()

    def __repr__(self):
        return "{}('{directory}', {_callback.__qualname__})".format(self.__class__.__qualname__, **self.__dict__)
    
    @asyncio.coroutine
    def setup(self, *args, **kwargs):
        yield from super().setup(*args, **kwargs)
        self.watch(alias='sms', path=self.directory, flags=aionotify.Flags.CREATE)
        self._task = self._loop.create_task(self.listen_to_inotify())
        self._task.add_done_callback(self._task_done)
        logger.debug('Leaving setup of %r', self)

    def _task_done(self, task):
        self._waiter.set_result(None)
        if not task.cancelled() and task.exception():
            logger.error('Error in Watcher task %r', task.exception())

    def scan_directory(self):
        for smsfile in glob.iglob(os.path.join(self.directory, '*.txt')):
            self._loop.call_soon(self._callback, smsfile)
            
    @asyncio.coroutine
    def stop(self):
        self._task.cancel()
        yield from self._waiter
        super().close()

    @asyncio.coroutine
    def listen_to_inotify(self):
        self.scan_directory()
        while True:
            event = yield from self.get_event()
            logger.debug('File event: %r', event.name)
            
            # If it's a directory, skip
            if event.flags & aionotify.Flags.ISDIR:
                logger.debug('Skip directory event: %r', event.name)
                continue
                
            self._loop.call_soon(self._callback, os.path.join(self.directory, event.name))
