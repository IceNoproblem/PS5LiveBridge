from __future__ import annotations

import asyncio
import sys
import time

from . import logbus

HEARTBEAT_TIMEOUT = 18000


class IrcClient(object):
    def __init__(self, reader, writer, server):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.nick = ""
        self.channel = ""
        self.peer = ""
        try:
            self.peer = "%s:%s" % writer.get_extra_info("peername")[:2]
        except Exception:
            self.peer = "?"
        self.last_active = time.time()
        self.alive = True
        self.joined = False

    def _check(self) -> bool:
        if not self.writer or self.writer.is_closing():
            self.alive = False
            return False
        if time.time() - self.last_active > HEARTBEAT_TIMEOUT:
            logbus.warn("PS5（%s）心跳超时，断开" % self.peer, tag="IRC")
            self.alive = False
            return False
        return True

    async def send(self, line: str) -> None:
        if not self._check():
            return
        if not line.endswith("\r\n"):
            line += "\r\n"
        try:
            self.writer.write(line.encode("utf-8", "ignore"))
            await self.writer.drain()
            self.last_active = time.time()
        except Exception:
            self.alive = False

    async def join_channel(self) -> None:
        if self.joined or not self._check():
            return
        target = "#" + (self.server.channel or "ps5")
        self.channel = target
        await self.send(":%s!%s@tmi.twitch.tv JOIN %s" % (self.nick, self.nick, target))
        await self.send(":tmi.twitch.tv 353 %s = %s :%s" % (self.nick, target, self.nick))
        await self.send(":tmi.twitch.tv 366 %s %s :End of /NAMES list" % (self.nick, target))
        self.joined = True
        logbus.ok("PS5 已进入弹幕频道 %s" % target, tag="IRC")
        self.server._fire_join(target)

    async def handle(self, line: str) -> None:
        if not line or not self._check():
            return
        parts = line.split()
        if not parts:
            return
        cmd = parts[0].upper()
        self.last_active = time.time()

        if cmd == "NICK" and len(parts) >= 2:
            self.nick = parts[1]
            logbus.info("PS5 IRC 昵称：%s" % self.nick, tag="IRC")

        elif cmd == "USER":
            n = self.nick or "ps5"
            for num, text in (
                ("001", "Welcome to the Twitch IRC Server!"),
                ("002", "Your host is tmi.twitch.tv"),
                ("003", "This server is rather new"),
                ("004", "tmi.twitch.tv -"),
                ("375", "-"),
                ("372", "You are in a maze of twisty passages, all alike."),
                ("376", ">"),
            ):
                await self.send(":tmi.twitch.tv %s %s :%s" % (num, n, text))
            await self.join_channel()

        elif cmd == "CAP":
            if len(parts) >= 2:
                sub = parts[1].upper()
                if sub == "LS":
                    await self.send(":tmi.twitch.tv CAP * LS :twitch.tv/tags "
                                    "twitch.tv/commands twitch.tv/membership")
                elif sub == "REQ":
                    caps = " ".join(parts[2:]) if len(parts) > 2 else ""
                    await self.send(":tmi.twitch.tv CAP * ACK :%s" % caps)
                elif sub == "END":
                    pass
            else:
                await self.send(":tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands")

        elif cmd == "PING":
            arg = parts[1] if len(parts) >= 2 else "tmi.twitch.tv"
            arg = arg.lstrip(":")
            await self.send("PONG :%s" % (arg or "tmi.twitch.tv"))

        elif cmd == "JOIN":
            if len(parts) >= 2:
                ch = parts[1]
                want = ch.lstrip(":#")
                if want and want != self.server.channel:
                    logbus.info("PS5 指定频道 %s，后续弹幕发往这里" % ch, tag="IRC")
                    self.server.channel = want
                await self.send(":%s!%s@tmi.twitch.tv JOIN %s" % (self.nick, self.nick, ch))
                await self.send(":tmi.twitch.tv 353 %s = %s :%s" % (self.nick, ch, self.nick))
                await self.send(":tmi.twitch.tv 366 %s %s :End of /NAMES list" % (self.nick, ch))
                self.channel = ch

        elif cmd == "PART":
            logbus.info("PS5 离开频道 %s" % (parts[1] if len(parts) > 1 else ""), tag="IRC")

        elif cmd == "WHO":
            if len(parts) >= 2:
                ch, n = parts[1], self.nick
                await self.send(":tmi.twitch.tv 352 %s %s %s 0.0.0.0 tmi.twitch.tv %s H :0 %s"
                                % (n, ch, n, n, n))
                await self.send(":tmi.twitch.tv 315 %s %s :End of WHO list" % (n, ch))

        elif cmd == "QUIT":
            self.alive = False

    async def run(self) -> None:
        try:
            while self._check():
                try:
                    data = await asyncio.wait_for(self.reader.readline(), timeout=15.0)
                except asyncio.TimeoutError:
                    await self.send("PING :tmi.twitch.tv")
                    continue
                if not data:
                    break
                line = data.decode("utf-8", "ignore").strip()
                if line:
                    await self.handle(line)
        except Exception as e:
            logbus.warn("PS5 IRC 连接异常：%s" % e, tag="IRC")
        finally:
            self.alive = False
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.server._drop(self)
            logbus.info("PS5 IRC 连接断开（%s）" % self.peer, tag="IRC")


class IrcServer(object):
    """只负责协议，不自带事件循环 —— 循环由 services.py 统一持有。"""

    def __init__(self):
        self.clients = []
        self.channel = "ps5"
        self.running = False
        self._server = None
        self.on_join = None
        self.on_drop = None

    async def start(self, host: str = "0.0.0.0", port: int = 6667) -> bool:
        use_reuse = (sys.platform != "win32") and hasattr(__import__("socket"), "SO_REUSEPORT")
        try:
            self._server = await asyncio.start_server(
                self._handle, host, port, reuse_address=True, reuse_port=use_reuse)
        except (ValueError, OSError) as e:
            if use_reuse:
                try:
                    self._server = await asyncio.start_server(
                        self._handle, host, port, reuse_address=True)
                except OSError as e2:
                    logbus.error("IRC 端口 %d 绑定失败：%s" % (port, e2), tag="IRC")
                    return False
            else:
                logbus.error("IRC 端口 %d 绑定失败：%s" % (port, e), tag="IRC")
                return False
        self.running = True
        logbus.info("弹幕通道已监听 %s:%d" % (host, port), tag="IRC")
        asyncio.ensure_future(self._serve())
        return True

    async def _serve(self):
        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logbus.error("IRC 服务异常：%s" % e, tag="IRC")

    async def stop(self):
        self.running = False
        for c in list(self.clients):
            c.alive = False
            try:
                c.writer.close()
            except Exception:
                pass
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
        self.clients = []

    async def _handle(self, reader, writer):
        c = IrcClient(reader, writer, self)
        self.clients.append(c)
        logbus.info("PS5 已连上弹幕通道（%s）" % c.peer, tag="IRC")
        await c.run()

    def _drop(self, client):
        try:
            self.clients.remove(client)
        except ValueError:
            pass
        if not self.clients and self.on_drop:
            try:
                self.on_drop()
            except Exception:
                pass

    def _fire_join(self, channel: str):
        if self.on_join:
            try:
                self.on_join(channel)
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return any(c.alive for c in self.clients)

    def _active(self):
        for c in list(self.clients):
            if c.alive:
                return c
        return None

    def _format(self, user: str, text: str) -> str:
        safe = "".join(ch for ch in (user or "bili") if ch.isalnum() or ch in "_-") or "bili"
        target = "#" + (self.channel or "ps5")
        return ":%s!%s@tmi.twitch.tv PRIVMSG %s :%s" % (safe, safe, target, text)

    async def say(self, user: str, text: str) -> bool:
        c = self._active()
        if not c:
            return False
        await c.send(self._format(user, text))
        return True

    async def notify(self, text: str) -> bool:
        return await self.say("系统", text)
