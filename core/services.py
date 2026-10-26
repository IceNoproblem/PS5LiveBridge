# -*- coding: utf-8 -*-
"""
core/services.py —— 异步服务宿主

整个程序只有这一处拥有事件循环：
    弹幕 WebSocket 与 PS5 的 IRC 连接都是 asyncio 的活，
    而编排器、Flask 都是同步线程 —— 用 call_soon_threadsafe 搭一座桥，
    避免「到处 asyncio.run()」那种各自开循环、互相抢端口的写法。
"""
from __future__ import annotations

import asyncio
import threading

from . import danmaku, ircsrv, logbus


class Services(object):
    def __init__(self, bili, on_danmaku=None, on_ps5_join=None, on_ps5_drop=None):
        self.bili = bili
        self.loop = None
        self.thread = None
        self.irc = ircsrv.IrcServer()
        self.dm = danmaku.DanmakuClient(bili, on_message=on_danmaku,
                                        on_state=self._on_dm_state)
        self.on_ps5_join = on_ps5_join
        self.on_ps5_drop = on_ps5_drop
        self._started = False
        self._ready = threading.Event()
        self.dm_state = "idle"

        self.irc.on_join = self._ps5_join
        self.irc.on_drop = self._ps5_drop_notify

    # ──────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────
    def start(self, irc_port: int = 6667) -> bool:
        if self._started:
            return True
        self.thread = threading.Thread(target=self._run, args=(irc_port,),
                                       name="async-services", daemon=True)
        self.thread.start()
        self._ready.wait(6.0)
        self._started = True
        return self._started

    def _run(self, irc_port: int):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def boot():
            ok = await self.irc.start("0.0.0.0", irc_port)
            if not ok:
                logbus.warn("弹幕通道端口 %d 没能监听，PS5 弹幕将不可用" % irc_port, tag="IRC")
            self._ready.set()

        self.loop.create_task(boot())
        try:
            self.loop.run_forever()
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    def stop(self):
        if not self.loop:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop)
            fut.result(timeout=5)
        except Exception:
            pass
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass

    async def _shutdown(self):
        try:
            await self.dm.stop()
        except Exception:
            pass
        try:
            await self.irc.stop()
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # 桥：同步 -> 异步
    # ──────────────────────────────────────────────
    def call(self, coro, timeout: float = 0):
        """从同步线程提交协程。返回结果（timeout>0 时等结果）。"""
        if not self.loop:
            try:
                coro.close()
            except Exception:
                pass
            return None
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if timeout:
            try:
                return fut.result(timeout=timeout)
            except Exception:
                return None
        return fut

    # ──────────────────────────────────────────────
    # 弹幕
    # ──────────────────────────────────────────────
    def _on_dm_state(self, state: str):
        self.dm_state = state

    def start_danmaku(self, room_id: int) -> None:
        if not room_id:
            return
        self.call(self.dm.start(room_id))

    def stop_danmaku(self) -> None:
        self.call(self.dm.stop())

    @property
    def danmaku_connected(self) -> bool:
        return bool(self.dm.connected)

    # ──────────────────────────────────────────────
    # PS5 画面注入
    # ──────────────────────────────────────────────
    def _ps5_join(self, channel: str):
        logbus.ok("PS5 弹幕通道就绪（%s）" % channel, tag="IRC")
        if self.on_ps5_join:
            try:
                self.on_ps5_join(channel)
            except Exception:
                pass

    def _ps5_drop_notify(self):
        if self.on_ps5_drop:
            try:
                self.on_ps5_drop()
            except Exception:
                pass

    def inject(self, user: str, text: str) -> None:
        self.call(self.irc.say(user, text))

    def notify_ps5(self, text: str) -> None:
        self.call(self.irc.notify(text))

    def set_channel(self, channel: str) -> None:
        self.irc.channel = channel or "ps5"

    @property
    def ps5_connected(self) -> bool:
        return self.irc.connected
