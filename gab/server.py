import ssl
import logging
import datetime
import asyncio
import collections
from urllib.parse import urlparse, urlunparse

from gab.proto import parsemsg

log = logging.getLogger(__name__)

TERMINATOR = object()
RECONNECT_TIMEOUT = 10

# FIXME client and server need to drain queue before quitting


def build_subparser(subparser):
    subparser.add_argument("url", action="append")
    subparser.add_argument("--nick", default="gabber")
    subparser.add_argument("--listen", default="localhost")
    subparser.add_argument("--port", default=6666, type=int)


class Client(object):
    def __init__(self, connection):
        self.connection = connection
        self.outgoing = asyncio.Queue()
        self.connected = False

    def disconnect(self):
        self.send(TERMINATOR)
        self.connection.remove_client(self)

    def send(self, message):
        self.outgoing.put_nowait(message)

    async def handle_read(self, reader, writer):
        self.connected = True

        while self.connected:
            try:
                data = await reader.readline()
            except ConnectionResetError:
                break
            line = data.decode().strip()
            if not line:
                break
            log.debug("client read: %s", line)
            message = Message.parse(line)

            if message.command == "QUIT":
                break
            elif message.command in ["NICK", "USER"]:  # dont reauth
                continue

            self.connection.send(message)

        self.connected = False
        await writer.drain()
        writer.close()

    async def handle_write(self, writer):
        self.connected = True
        while self.connected:
            message = await self.outgoing.get()
            if message is TERMINATOR:
                break
            log.debug("client wrote: %s", message)
            writer.write(message.serialize())
        self.connected = False
        await writer.drain()
        writer.close()


class Server(object):
    def __init__(self, connection):
        self.connection = connection
        self.server = None

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def create(self, host, port):
        self.server = await asyncio.start_server(self.handle_client, host, port)

    def handle_client(self, reader, writer):
        client = Client(self.connection)

        log.debug("client connection from %s", writer.get_extra_info("peername"))

        asyncio.ensure_future(client.handle_read(reader, writer))
        asyncio.ensure_future(client.handle_write(writer))
        self.connection.add_client(client)


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
    def __init__(self, command, *args, prefix=None):
        self.timestamp = datetime.datetime.utcnow()
        self.prefix = prefix
        self.command = command
        self.args = args

    def __str__(self):
        return "<Message(prefix={}, command={}, args={})>".format(self.prefix or "", self.command, self.args)

    def serialize(self):
        parts = ([":" + self.prefix] if self.prefix else []) + [self.command]
        if self.args:
            parts.extend([str(arg) for arg in self.args[:-1]] + [":" + self.args[-1]])
        line = " ".join(parts) + "\r\n"
        return line.encode()

    @classmethod
    def parse(cls, message):
        prefix, command, args = parsemsg(message)
        return cls(command, *args, prefix=prefix)

    @classmethod
    def nick(cls, nick, **kwargs):
        return cls("NICK", nick, **kwargs)

    @classmethod
    def user(cls, user, realname=None, mode="i", **kwargs):
        return cls("USER", user, mode, "*", realname or user, **kwargs)

    @classmethod
    def privmsg(cls, target, msg, **kwargs):
        return cls("PRIVMSG", target, msg, **kwargs)

    @classmethod
    def quit(cls, msg, **kwargs):
        return cls("QUIT", msg, **kwargs)

    @classmethod
    def pong(cls, target, **kwargs):
        return cls("PONG", target, **kwargs)

    @classmethod
    def join(cls, channel, **kwargs):
        return cls("JOIN", channel, **kwargs)

    @classmethod
    def reply_namesreply(cls, nick, channel, **kwargs):
        names = " ".join([nick.name for nick in channel.members])
        return cls("353", nick, "=", channel.name, names, **kwargs)

    @classmethod
    def reply_endnamesreply(cls, nick, channel, **kwargs):
        return cls("366", nick, channel.name, "End of /NAMES list.", **kwargs)


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

        for client in self.connection.clients:
            client.send(message)

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
        self.connection.send(Message.pong(message.args[0]))

    def on_pong(self, message):
        pass

    def on_join(self, message):
        self.irc.join_channel(message.args[0])

    def on_part(self, message):
        self.irc.part_channel(message.args[0])

    # RPL_NAMREPLY
    def on_353(self, message):
        channel_name = message.args[2]
        channel = self.irc.get_channel(channel_name)
        if channel:
            nicks = message.args[3].split()
            for nick in nicks:
                channel.add_nick(Nickname(nick))


class MessageBuffer(object):
    def __init__(self):
        self.messages = []

    def clear(self):
        self.messages.clear()

    def add(self, message):
        self.messages.append(message)

    def __iter__(self):
        return iter(self.messages)


class Nickname(object):
    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, Nickname) and other.name == self.name


class Channel(object):
    def __init__(self, name):
        self.name = name
        self.members = []

    def __eq__(self, other):
        return isinstance(other, Channel) and other.name == self.name

    def add_nick(self, nickname):
        if nickname not in self.members:
            self.members.append(nickname)

    def remove_nick(self, nickname):
        if nickname in self.members:
            self.members.remove(nickname)


class IRC(object):
    def __init__(self):
        self.server_messages = MessageBuffer()
        self.clients = collections.OrderedDict()
        self.channels = collections.OrderedDict()

    def clear_messages(self):
        self.server_messages.clear()

    def get_channel(self, name):
        return self.channels.get(name)

    def get_channels(self):
        return self.channels.values()

    def join_channel(self, channel_name):
        channel = Channel(channel_name)
        self.channels[channel_name] = channel

    def part_channel(self, channel_name):
        if channel_name in self.channels:
            del self.channels[channel_name]

    def add_server_message(self, message):
        self.server_messages.add(message)


class IRCConnection(object):
    def __init__(self, url, nick, reconnect_timeout=RECONNECT_TIMEOUT):
        self.url = url
        self.host, self.port, self.ssl = parse_host_port(url)
        self.nick = nick
        self.outgoing = asyncio.Queue()
        self.clients = []
        self.irc = IRC()
        self.diconnecting = False
        self.connected = False
        self.reconnect_timeout = reconnect_timeout

    def disconnect(self, message="see-ya"):
        self.diconnecting = True
        self.send(Message.quit(message))

        for client in self.clients:
            client.disconnect()

        self.send(TERMINATOR)

    def add_client(self, client):
        self.clients.append(client)

        for message in self.irc.server_messages:
            client.send(message)

        for channel in self.irc.get_channels():
            client.send(Message.join(channel.name, prefix=self.nick))
            client.send(Message.reply_namesreply(self.nick, channel, prefix=self.host))
            client.send(Message.reply_endnamesreply(self.nick, channel, prefix=self.host))

    def remove_client(self, client):
        if client in self.clients:
            self.clients.remove(client)

    def send(self, message):
        self.outgoing.put_nowait(message)

    async def connect(self):
        do_ssl = ssl._create_unverified_context() if self.ssl else False

        reader = writer = None
        while not self.connected:
            log.info("connecting to %s:%s...", self.host, self.port)

            try:
                reader, writer = await asyncio.open_connection(host=self.host, port=self.port, ssl=do_ssl)
            except ConnectionError as e:
                log.error("could not connect to %s: '%s'. Reconnecting in %s seconds...", self.url, e, self.reconnect_timeout)
                await asyncio.sleep(self.reconnect_timeout)
                continue

            self.connected = True

        log.info("connected")

        self.irc.clear_messages()

        asyncio.ensure_future(self.handle_read(reader))
        asyncio.ensure_future(self.handle_write(writer))

        self.send(Message.nick(self.nick))
        self.send(Message.user(self.nick))

    async def handle_write(self, writer):
        while self.connected:
            message = await self.outgoing.get()
            if message is TERMINATOR:
                break
            log.debug("upstream write: %s", message)
            writer.write(message.serialize())

        writer.close()

    async def handle_read(self, reader):
        handler = MessageHandler(self, self.irc)

        while self.connected:
            try:
                data = await reader.readline()
            except ConnectionResetError:
                break
            line = data.decode().strip()
            if not line:
                break

            log.debug("upstream read: %s", line)
            message = Message.parse(line)
            handler.dispatch(message)
        self.connected = False

        if not self.diconnecting:
            log.info("reconnecting in %s seconds", self.reconnect_timeout)
            await asyncio.sleep(self.reconnect_timeout)
            await self.connect()


def connect_to_network(url, nick, server_address):
    connection = IRCConnection(url, nick)
    asyncio.ensure_future(connection.connect())

    server = Server(connection)
    asyncio.ensure_future(server.create(*server_address))

    return connection, server


def main(args):
    log.info("server go")

    loop = asyncio.get_event_loop()

    urls = [urlparse(u) for u in args.url]
    connections = []
    servers = []
    for i, url in enumerate(urls):
        address = (args.listen, args.port + i)
        connection, server = connect_to_network(url, args.nick, address)
        connections.append(connection)
        servers.append(server)
        log.info("%s listening on %s", url.geturl(), address)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        log.info("Shutdown...")

    for server in servers:
        loop.run_until_complete(server.close())

    for connection in connections:
        connection.disconnect()

    pending = asyncio.Task.all_tasks()
    loop.run_until_complete(asyncio.gather(*pending))

    return 0
