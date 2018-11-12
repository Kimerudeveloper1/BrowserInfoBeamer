#!/usr/bin/python2.7
import os
import re
import sys
import time
import urllib
import websocket
import requests
import traceback
import select
import json
import socket
from itertools import count, cycle

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
def send_ib(data):
    sock.sendto(data, ('127.0.0.1', 4444))

http = requests.Session()

class EventLoop(object):
    def __init__(self):
        self._poller = select.poll()
        self._fd_to_handler = {}
    
    def register(self, fd, handler):
        self._fd_to_handler[fd] = handler
        self._poller.register(fd, select.POLLIN)

    def unregister(self, fd):
        self._poller.unregister(fd)
        del self._fd_to_handler[fd]

    def dispatch(self, timeout_ms):
        while 1:
            events = self._poller.poll(timeout_ms)
            if not events:
                break
            for fd, event in events:
                handler = self._fd_to_handler[fd]
                try:
                    handler()
                except:
                    traceback.print_exc()

class Tab(object):
    def __init__(self, eventloop, page):
        self._connection = None
        self._fd = None
        self._next_id = count().next
        self._eventloop = eventloop
        self._loaded = True
        self._scripts = None
        self._socket_url = None
        self._frame = 0
        self._url = ''
        self._injects = 0
        self.update(page)

    def update(self, page):
        # print page
        self._id = page['id']
        self._url = page['url']
        if not self._socket_url:
            # Might be empty in /json/list
            self._socket_url = page['webSocketDebuggerUrl']
        self.ensure_connected()
        self.call_rpc("Page.enable")
        # self.call_rpc("Emulation.setDeviceMetricsOverride",
        #     width = 1280,
        #     height = 720,
        #     deviceScaleFactor = 0,
        #     scale = 2,
        #     screenWidth = 1280,
        #     mobile = False,
        #     fitWindow = True,
        # )
        # self.call_rpc("Network.enable")
        self.call_rpc("DOM.enable")


    def ensure_connected(self):
        if self._connection:
            return
        try:
            self._connection = websocket.create_connection(self._socket_url)
            self._fd = self._connection.fileno()
        except Exception:
            traceback.print_exc()
            return
        print >>sys.stderr, "websocket connected"
        self._eventloop.register(self._fd, self.receive_rpc)

    def reset_connection(self):
        print >>sys.stderr, "websocket disconnected"
        self._eventloop.unregister(self._fd)
        self._connection.close()
        self._connection = None

    def call_rpc(self, name, **args):
        self.ensure_connected()
        rpc = json.dumps({
            "id": self._next_id(),
            "method": name,
            "params": args,
        })
        print >>sys.stderr, ">>> %s %s" % (self._id, rpc)
        try:
            self._connection.send(rpc)
        except websocket.WebSocketConnectionClosedException:
            self.reset_connection()
        except Exception:
            traceback.print_exc()
            self.reset_connection()

    @property
    def is_loaded(self):
        return self._loaded

    def rpc_page_framenavigated(self, frame):
        # top level navigation finished. Save url
        if not 'parentId' in frame:
            self._frame = frame['id']
            self._url = frame['url']

    def rpc_page_loadeventfired(self, timestamp, **kwargs):
        self._loaded = True
        if self._injects >= 3:
            print >>sys.stderr, "TOO MANY INJECTS"
            return
        self._injects += 1
        script = self._scripts.get_script(self._url)
        if script:
            print >>sys.stderr, "RUNNING SCRIPT:\n" + script
            self.call_rpc("Runtime.evaluate", expression=script)

    def receive_rpc(self):
        try:
            response = json.loads(self._connection.recv())
            print >>sys.stderr, "<<< %r" % response
            # pprint.pprint(response)
            if 'id' in response:
                return
            method = response['method']
            params = response.get('params', {})
            mangled = "rpc_%s" % re.sub("[^\w]", "_", method.lower())
            handler = getattr(self, mangled, None)
            if handler:
                handler(**params)
        except Exception:
            traceback.print_exc()
            self.reset_connection()

    def navigate(self, url, scripts):
        self._loaded = False
        self._frame = 0
        self._url = ''
        self._injects = 0
        self._scripts = scripts
        self.call_rpc("Page.navigate", url=url)

class Browser(object):
    def __init__(self, eventloop, base_url="http://127.0.0.1:9222"):
        self._base_url = base_url
        self._eventloop = eventloop
        self._tabs = []
        self._tab_by_id = {}
        self.update_tabs()

    def update_tabs(self):
        self._tabs = []
        for prio, page in enumerate(http.get(self._base_url + "/json/list").json()):
            # pprint.pprint(page)
            if page['type'] != 'page':
                continue
            page['prio'] = prio
            id = page['id']
            if not id in self._tab_by_id:
                self._tab_by_id[id] = Tab(self._eventloop, page)
            else:
                self._tab_by_id[id].update(page)
            self._tabs.append(id)
        # pprint.pprint(self._tabs)

    @property
    def tabs(self):
        return self._tabs

    def open(self, url):
        page = http.get((self._base_url + "/json/new?%s") % urllib.quote(url)).json()
        id = page['id']
        self._tab_by_id[id]= Tab(self._eventloop, page)
        self._tabs.append(id)
        return id

    def navigate(self, id, url, script):
        print >>sys.stderr, "=== Navigate %s -> %s" % (id, url)
        self._tab_by_id[id].navigate(url, script)

    def switch_to(self, id):
        print >>sys.stderr, "=== Switch %s" % id
        http.get(self._base_url + "/json/activate/%s" % id)
        self.update_tabs()

    def is_loaded(self, id):
        return self._tab_by_id[id].is_loaded

    def close(self, id):
        if len(self._tabs) == 1:
            return
        http.get(self._base_url + "/json/close/%s" % id).content
        self.update_tabs()


e = EventLoop()
b = Browser(e)

FALLBACK = cycle([
    (10, 'about:blank', '')
]).next

class Scripts(object):
    def __init__(self, config):
        self._scripts = []
        for pattern, script in config:
            self._scripts.append((
                re.compile(pattern).match,
                script,
            ))
        print >>sys.stderr, "loaded %d scripts" % len(self._scripts)

    def get_script(self, url):
        print >>sys.stderr, "WANT SCRIPT (%s)" % url
        for matcher, script in self._scripts:
            if matcher(url):
                return script
        return None

class Configuration(object):
    def __init__(self, config_json):
        self._urls = FALLBACK
        self._config_json = config_json
        self._prefix = os.path.dirname(config_json)
        self._mtime = 0

    def load_config(self):
        with file(self._config_json) as f:
            config = json.load(f)
            with file(os.path.join(self._prefix, config['scripts']['asset_name'])) as s:
                scripts = Scripts(json.load(s))

            urls = config['urls']
            if urls:
                self._urls = cycle([
                    (item['duration'], item['url'], scripts)
                    for item in urls
                    if item['duration'] > 0
                ]).next
            else:
                self._url = FALLBACK

    def next_item(self):
        try:
            mtime = os.stat(self._config_json).st_mtime
            if mtime != self._mtime:
                self.load_config()
                self._mtime = mtime
        except Exception:
            traceback.print_exc()

        return self._urls()

config_json = '../../config.json'
if len(sys.argv) == 2:
    config_json = sys.argv[1]

c = Configuration(config_json)

def ensure_two_tabs():
    b.update_tabs()
    while len(b.tabs) < 2:
        b.open("about:blank")
    while len(b.tabs) > 2:
        b.close(b.tabs[2])

ensure_two_tabs()
duration, url, scripts = c.next_item()
print >>sys.stderr, "initial url", url

if 0:
    b.navigate(b.tabs[0], url, scripts)
    next_switch = time.time() + 9999
else:
    b.navigate(b.tabs[1], url, scripts)
    next_switch = time.time() + 10

while 1:
    ensure_two_tabs()

    for i in xrange(10):
        # print [b.is_loaded(id) for id in b.tabs]
        e.dispatch(100)

    now = time.time()
    if (b.is_loaded(b.tabs[1]) and now > next_switch) or now > next_switch + 5:
        print >>sys.stderr, "=== Now switching to TAB[1]"
        b.switch_to(b.tabs[1])
        send_ib('root/fade:1')
        next_switch = now + duration
        duration, url, scripts = c.next_item()
        print >>sys.stderr, "=== Loading %s in TAB[1] next switch %d (in %ds)" % (
            url, next_switch, next_switch - time.time()
        )
        b.navigate(b.tabs[1], url, scripts)
