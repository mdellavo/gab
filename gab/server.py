import logging
import datetime
import asyncio
import collections
from urllib.parse import urlparse

from gab.proto import parsemsg

log = logging.getLogger(__name__)


def build_subparser(subparser):
    subparser.add_argument("url", action="append")


class Client(object):
    def __init__(self, reader, writer):
        self.outgoing = asyncio.Queue()
        asyncio.ensure_future(self.handle_read(reader))
        asyncio.ensure_future(self.handle_write(writer))

    async def handle_read(self, reader):
        pass

    async def handle_write(self, writer):
        pass


def handle_client(reader, writer):
    Client(reader, writer)


def parse_host_port(url):

    host = url.netloc
    ssl = False
    if ":" in url.netloc:
        host, port_s = url.netloc.split(":")
        port = int(port_s)
    elif url.scheme == "ircs":
        port = 9999
        ssl = True
    else:
        port = 6667
    return host, port, ssl


class Message(object):
    def __init__(self, prefix, command, args):
        self.timestamp = datetime.datetime.utcnow()
        self.prefix = prefix
        self.command = command
        self.args = args

    def __str__(self):
        return "<Message(prefix={}, command={}, args={})>".format(self.prefix, self.command, self.args)

    @classmethod
    def parse(cls, message):
        prefix, command, args = parsemsg(message)
        return cls(prefix, command, args)


class MessageHandler(object):
    def __init__(self, connection, irc):
        self.connection = connection
        self.irc = irc

    def dispatch(self, message):
        handler_name = "on_" + message.command.lower()
        handler = getattr(self, handler_name, None)
        if handler:
            handler(message)
        else:
            log.debug("no handler for message %s", message)

    def gather_server_message(self, message):
        self.irc.add_server_message(message)

    on_001 = gather_server_message
    on_002 = gather_server_message
    on_003 = gather_server_message
    on_004 = gather_server_message
    on_005 = gather_server_message

    on_251 = gather_server_message
    on_252 = gather_server_message
    on_253 = gather_server_message
    on_254 = gather_server_message
    on_255 = gather_server_message

    on_265 = gather_server_message
    on_266 = gather_server_message

    on_375 = gather_server_message
    on_372 = gather_server_message
    on_376 = gather_server_message

    on_notice = gather_server_message
    on_mode = gather_server_message

    def on_ping(self, message):
        self.connection.send("PONG", message.args[0])


class Buffer(object):
    def __init__(self):
        self.messages = []

    def add(self, message):
        self.messages.append(message)


class IRC(object):
    def __init__(self, connection):
        self.connection = connection
        self.server_messages = Buffer()
        self.clients = collections.OrderedDict()

    def add_server_message(self, message):
        self.server_messages.add(message)


class Connection(object):
    def __init__(self, url, nick):
        self.url = url
        self.nick = nick
        self.outgoing = asyncio.Queue()

        self.handler = MessageHandler(self, IRC(self))

    def send(self, command, *args):
        parts = [command]
        if args:
            parts.extend([str(arg) for arg in args[:-1]] + [":" + args[-1]])
        message = " ".join(parts)
        self.outgoing.put_nowait(message)

    async def connect(self):
        host, port, ssl = parse_host_port(self.url)
        log.info("connecting to %s:%s...", host, port)
        reader, writer = await asyncio.open_connection(host=host, port=port, ssl=ssl)
        log.info("connected")

        asyncio.ensure_future(self.handle_read(reader))
        asyncio.ensure_future(self.handle_write(writer))

        self.send("NICK", self.nick)
        self.send("USER", self.nick, "i", "*", self.nick)

    async def handle_write(self, writer):
        while True:
            message = await self.outgoing.get()
            if not message:
                break
            data = (message + "\r\n").encode()
            writer.write(data)

    async def handle_read(self, reader):
        while True:
            data = await reader.readline()
            line = data.decode().strip()
            if not line:
                break

            message = Message.parse(line)
            self.handler.dispatch(message)


def main(args):
    log.info("server go")

    loop = asyncio.get_event_loop()

    urls = [urlparse(u) for u in args.url]
    connections = [Connection(url, "gabber") for url in urls]
    for connection in connections:
        asyncio.ensure_future(connection.connect())

    coro = asyncio.start_server(handle_client, '127.0.0.1', 6666)
    server = loop.run_until_complete(coro)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        log.info("Shutdown...")

    server.close()
    loop.run_until_complete(server.wait_closed())
    loop.close()

    return 0
