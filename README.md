# verite
Client and server code for the show #Vérité

This repo contains Python and Kivy files for the server, client and client GUI of the theatrical show #Vérité (see the theater company http://www.quincailleriemoderne.fr/).

The server is intended to be used on a computer running Linux (e.g. Raspberry Pi) with a USB 3G dongle. It uses the inotify system to listen to a folder, specifically an SMS inbox folder, as used by gammu-smsd. It categorizes each received SMS as a vote or a message by a regex match and responds to several client requests.

The client is intended to be run on a computer in the same local network. It communicates in TCP with the server and allows the user to triage the received messages, create his own, start and stop polls, and send SMS to all or part of the phonebook. It also displays all the session events (polls, votes and messages) in a chronological order as well as the current phonebook.
The server stores every information for a session so that on client restart, the session is restored as is (except the phonebook informations).

Missing from this repo :
- the server graphical display, written in C++ by another programmer. This display only reacts to OSC messages sent by the server and does not send back any data/event/whatsoever.
- the mandatory verite.conf configuration file, which includes sensitive information such as the account id and password for the SMS service used for the show.

This project depends on kivy, aiohttp, aionotify, netifaces, python-osc, configobj and validate.
The server initially runned on Python 3.4
The client initially runned on Python 3.5
