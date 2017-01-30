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


import kivy
kivy.require('1.9.1')

# Hack to interface standard python logging with kivy's custom logging
# See http://stackoverflow.com/questions/36106353/using-python-logging-when-logging-root-has-been-redefined-kivy
from kivy.logger import Logger
import logging
logger = logging.getLogger('SMS GUI Client')
logging.Logger.manager.root = logger
#logging.Logger.manager.root = Logger
#logging.root = logger


from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.core.window import Window
from kivy.properties import ListProperty, DictProperty, StringProperty, ObjectProperty, NumericProperty, BooleanProperty
from kivy.uix.label import Label
from kivy.uix.widget import Widget
from kivy.uix.scrollview import ScrollView
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.stacklayout import StackLayout
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.behaviors.togglebutton import ToggleButtonBehavior

# Standard library
import sys
import time
import os.path
import argparse
import asyncio
from threading import Thread

# Application
import utils
from SMS_client import Client
from utils import CLIENT_ID

DIRECTORY = os.path.dirname(os.path.realpath(__file__))

DEFAULT_LOGLEVEL = 'INFO'
DEFAULT_CONFIGNAME = 'verite.conf'
SPECFILE_EXT = '.spec'
DEFAULT_CONFIGFILE = os.path.join(DIRECTORY, DEFAULT_CONFIGNAME)

RGB_COLORS = {'black': [0, 0, 0], 
              'white': [1, 1, 1], 
              'grey': [.3, .3, .3], 
              'green': [0, .6, 0], 
              'dark_green': [0, .5, .1], 
              'dark_green2': [0, .4, 0], 
              'red': [.6, 0, 0], 
              'dark_red': [.5, 0, 0], 
              'dark_red2': [.4, 0, 0], 
              'blue': [0, .2, .6], 
             }
              
RGBA_COLORS = {name: values + [1] for name, values in RGB_COLORS.items()}


class KivyLogHandler(logging.Handler):
    def emit(self, record):
        self.format(record)
        msg = record.name + ':' + record.message
        Logger.log(record.levelno, msg)
        
logger.addHandler(KivyLogHandler())


class Tooltip(Label):
    pass


class TooltipBehavior(Widget):
    _tooltip = StringProperty()


class SMS(StackLayout):
    _id = NumericProperty()
    phone = StringProperty()
    content = StringProperty()
    to_display = BooleanProperty()
    displayed = BooleanProperty()


class Voter(Label, ToggleButtonBehavior, TooltipBehavior):
    phone = StringProperty()
    pedigree = StringProperty('?')
    color = ListProperty(RGB_COLORS['black'])
    send_status = StringProperty()
    enabled = BooleanProperty(True)
    
    def on_send_status(self, instance, value):
        self.color = RGB_COLORS['dark_green2'] if value.isdigit() else RGB_COLORS['dark_red2']
        self._tooltip = str(value)
        
    def on_state(self, instance, value):
        self.enabled = (value == 'normal')
            
        
class EventWidget(Label, TooltipBehavior):
    color = ListProperty(RGB_COLORS['white'])
    
    def __init__(self, timestamp, phone, data, type, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timestamp = timestamp[-6:-4] + 'h' + timestamp[-4:-2] + 'm' + timestamp[-2:] +'s'
        self.phone = phone
        self.type = type
        self.data = data
        if type == utils.EventTypes.vrai:
            color = RGB_COLORS['dark_green']
        elif type == utils.EventTypes.faux:
            color = RGB_COLORS['dark_red']
        elif type == utils.EventTypes.message:
            color = RGB_COLORS['blue']
        elif type == utils.EventTypes.sondage:
            color = RGB_COLORS['black']
        elif type == utils.EventTypes.fin_sondage:
            color = RGB_COLORS['grey']
            
        self.text = self.timestamp + '    ' + self.phone + '    ' + self.data
        self.color = color
    
    
class SMSClient(BoxLayout):
    available = ObjectProperty()
    displayed = ObjectProperty()
    popup = ObjectProperty()
    tooltip = ObjectProperty()
    messages = DictProperty()
    timeline = ListProperty()
    timeline_view = ObjectProperty()
    phonebook = DictProperty()
    phonebook_view = ObjectProperty()
    connected = BooleanProperty(False)
    connection_status = StringProperty('Server not connected')
    sms_service_connected = BooleanProperty(False)
    sms_service_status = StringProperty('SMS Service not contacted')
    
    def create_message(self, input):
        content = input.text
        if content:
            input.text = ''
            logger.debug('SEND OSC New Message with content %r', content)
            self.send_osc('/newmessage', [content])
        
    def delete_message(self, sms):
        id = sms._id
        sms.parent.remove_widget(sms)
        if id in self.messages:
            del self.messages[id]
        else:
            logger.error('Deletion of SMS not in messages: %r, %r', id, sms.content)
        self.send_osc('/delete', [id])
        logger.debug('DELETE Message id: %r', id)
        
    def change_column(self, sms):
        to_display = not sms.to_display
        sms.to_display = to_display
        neighbour = self.displayed if to_display else self.available
        sms.parent.remove_widget(sms)
        neighbour.add_widget(sms)
        self.send_osc('/to_display', [sms._id, to_display])
        
    def set_pedigree(self, sms):
        if sms.phone != CLIENT_ID:
            self.phonebook[sms.phone].pedigree = sms.content
        self.delete_message(sms)
        
    def set_displayed(self):
        for sms in self.available.children:
            sms.displayed = False
        for sms in self.displayed.children:
            sms.displayed = True
        # Children are stored from bottom to top
        ids = [sms._id for sms in reversed(self.displayed.children)]
        self.send_osc('/messages', ids)
        logger.debug('DISPLAY: %r', ids)
        
    def start_poll(self, input, poll_chrono):
        title = input.text
        if title:
            input.text = ''
            chrono = float(poll_chrono.text or 0)
            chrono = chrono if chrono > 0 else 0
            logger.debug('SONDAGE with title %r, chrono %r', title, chrono)
            self.send_osc('/sondage', [title, chrono])
            
    def stop_poll(self):
        self.send_osc('/finsondage')
        logger.debug('FIN SONDAGE')
        
    def send_sms(self, popup, sender_input, sms_input):
        sender = sender_input.text
        content = sms_input.text
        phonelist = [phone for phone, voter in self.phonebook.items() if voter.enabled]
        popup.dismiss()
        if content and sender and phonelist:
            logger.info('Sending SMS with sender: %r content: %r', sender, content)
            self._client.send_sms(sender, content, phonelist, self.sms_callback)
            sms_input.text = ''
            
    @mainthread
    def sms_callback(self, future):
        try:
            result = future.result()
            for phone, res in result.items():
                res = res.__repr__() if isinstance(res, Exception) else res.replace('\n', '00')
                result[phone] = res
                self.phonebook[phone].send_status = res
            logger.info('GOT SMS RESPONSE: %r', result)
        except Exception as e:
            result = e
            logger.error('SMS RESPONSE ERROR: %r', result)
            
    def get_credits(self, *args):
        self._client.get_credits(self.credits_callback)
        
    @mainthread
    def credits_callback(self, future):
        try:
            result = future.result()
            self.sms_service_connected = True
            self.sms_service_status = 'Highpush credits API responding'
            logger.debug('Credits retrieved: %r', result)
        except Exception as e:
            result = e
            self.sms_service_connected = False
            self.sms_service_status = 'Highpush credits unavailable: {}'.format(e.__repr__())
            logger.error('Credits callback failed with error: %r', result)

    @mainthread
    def process_event(self, payload, timestamp):
        event = utils.Event(*payload)
        phone = event.phone
        if phone != CLIENT_ID and phone not in self.phonebook:
            voter = Voter(phone=phone)
            self.phonebook[phone] = voter
            self.phonebook_view.add_widget(voter)
        widget = EventWidget(*event)
        if phone != CLIENT_ID:
            widget._tooltip = self.phonebook[phone].pedigree
            self.phonebook[phone].bind(pedigree=widget.setter('_tooltip'))
        else:
            widget._tooltip = 'client'
        index = utils.insort_record(self.timeline, event)
        self.timeline_view.add_widget(widget, index=index)
        logger.debug('GOT EVENT %r from phone %r', event, phone)
        
    @mainthread
    def process_message(self, payload, timestamp):
        _id, phone, content, to_display, displayed = payload
        phone = phone or ''
        sms = SMS(_id=_id, phone=phone, content=content, to_display=to_display, displayed=displayed)
        self.messages[_id] = sms
        column = self.displayed if to_display else self.available
        column.add_widget(sms)
        logger.debug('ADD Message _id: %r, content: %r', _id, content)
        
        
    @mainthread
    def connection_state(self, state, **kwargs):
        self.connected = state
        if state:
            self.connection_status = 'Connected with Server:\n{}'.format(kwargs['peername'])
        else:
#            TODO: if server restarts, how to keep show data consistent ?
            self.connection_status = 'Server disconnected:\n{}'.format(kwargs['exc'])
            
    def send_osc(self, address, params=[]):
        self._client.request_server(address, params)
            
    def _start_thread(self, loop, config):
        callbacks = {'process_event': self.process_event, 
                     'process_message': self.process_message, 
                     'connection_state_cb': self.connection_state, 
                     }
        self._asyncio_loop = loop
        self._thread = Thread(target=self._thread_job, args=(loop, config, callbacks), name='Client Asyncio Thread')
        self._thread.start()
        
        self.tooltip = Tooltip()
        self.schedule_tooltip = Clock.create_trigger(self.display_tooltip, 1)
        Window.bind(mouse_pos=self.on_motion)
        Window.bind(on_motion=self.on_motion)
        
        Clock.schedule_once(self.get_credits, 1)
        Clock.schedule_interval(self.get_credits, 120)

    def on_motion(self, *args):
        Window.remove_widget(self.tooltip) # close if it's opened
        self.schedule_tooltip.cancel() # cancel scheduled event since the cursor moved
        self.schedule_tooltip()

    def display_tooltip(self, *args):
        pos = Window.mouse_pos
        t = self.tooltip
        
        text = ''
        next_ = []
        candidates = [self]
        while candidates and text == '':
            for child in (child for widget in candidates for child in widget.children):
                point = pos if isinstance(child, ScrollView) else child.to_widget(*pos)
                if child.collide_point(*point):
                    if hasattr(child, '_tooltip'):
                        text = child._tooltip
                        break
                    next_.append(child)
            candidates, next_ = next_, []
            
        if text == '':
            return
        t.text = text
        t.text_size = None, None
        t.texture_update()
        if t.texture_size[0] > Window.width / 2:
            t.text_size = Window.width / 2, None
            t.texture_update()
        t.x = pos[0] if pos[0] + t.width <= Window.width else pos[0] - t.width
        t.y = pos[1] if pos[1] + t.height <= Window.height else pos[1] - t.height
        Window.add_widget(self.tooltip)
        
    def _thread_job(self, loop, config, callbacks):
        logger.debug('STARTING asyncio thread')
        asyncio.set_event_loop(loop)
        with Client(loop, config, callbacks) as client:
            self._client = client
            loop.run_forever()
        logger.debug('STOPPED asyncio thread')


class SMSClientApp(App):
#    TODO: handle KeyboardInterrupt properly    
#    TODO: transform asyncio callbacks into a Queue
    def __init__(self, config, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config = config
        loop = asyncio.get_event_loop()
        loop.set_debug(True)
        self._asyncio_loop = loop
        
    def build(self):
        return SMSClient()
        
    def on_start(self):
        self._filename = 'Verite_Phonebook_' + time.strftime('%Y-%m-%d_%Hh%M')
        self.root._start_thread(self._asyncio_loop, self._config)
    
    def on_stop(self):
        if self._asyncio_loop.is_running():
            self._asyncio_loop.call_soon_threadsafe(self._asyncio_loop.stop)
        self.root._thread.join()
        
#        directory = os.path.join(os.path.abspath(self.directory), 'phonebooks')
#        if not os.path.exists(directory):
#            try:
#                os.mkdir(directory)
#            except OSError as e:
#                logger.error("Couldn't create directory %r: %r", directory, e)
#        try:
#            relpath = os.path.join(directory, self._filename)
#            with open(relpath, 'w') as f:
#                for phone in self.root.phonebook:
#                    f.write(phone + '\n')
#        except OSError as e:
#            logger.error("Couldn't write phonebook to file %r: %r", relpath, e)
#        logger.debug('Phonebook written to %r', os.path.abspath(relpath))
        
        
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SMS Server for the show #Vérité')
    parser.add_argument('-l', '--loglevel', type=str, choices=list(logging._nameToLevel), default=DEFAULT_LOGLEVEL)
    parser.add_argument('-c', '--configfile', type=open, default=DEFAULT_CONFIGFILE)
    parser.add_argument('-s', '--specfile', type=open)
    args = parser.parse_args()
#    logging.basicConfig(level=args.loglevel) #, format='%(levelname)s:%(name)s:%(message)s')
    Logger.setLevel(args.loglevel)
    logger.setLevel(args.loglevel)
    
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
            
    SMSClientApp(config).run()
