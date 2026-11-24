from __future__ import annotations

import asyncio
import threading

from . import danmaku, danmaku_douyin, danmaku_douyu, ircsrv, logbus


class Services(object):
    def __init__(self, bili, on_danmaku=None, on_ps5_join=None, on_ps5_drop=None):
        self.bili = bili
        self.loop = None
        self.thread = None
        self.irc = ircsrv.IrcServer()
        self.dm = danmaku.DanmakuClient(bili, on_message=on_danmaku,
                                        on_state=self._on_dm_state)
        # 斗鱼弹幕不要账号，填个房间号就能拉，所以单独一份，
        # 跟 B 站登录无关，各走各的配置
        self.dm_douyu = danmaku_douyu.DouyuDanmaku(
            on_message=on_danmaku, on_state=self._on_dm_state_douyu)
        # 抖音同样不要账号，但握手要签名和 cookie，模块内部自己处理
        self.dm_douyin = danmaku_douyin.DouyinDanmaku(
            on_message=on_danmaku, on_state=self._on_dm_state_douyin)
        self.on_ps5_join = on_ps5_join
        self.on_ps5_drop = on_ps5_drop
        self._started = False
        self._ready = threading.Event()
        self.dm_state = "idle"
        self.dm_douyu_state = "idle"
        self.dm_douyu_room = ""
        self.dm_douyin_state = "idle"
        self.dm_douyin_room = ""

        self.irc.on_join = self._ps5_join
        self.irc.on_drop = self._ps5_drop_notify

    def start(self, irc_port: int = 6667) -> bool:
        if self._started:
            return True
        self.thread = threading.Thread(target=self._run, args=(irc_port,),
                                       name="async-services", daemon=True)
        self.thread.start()
        self._ready.wait(6.0)
        # 就绪事件置位只表示 boot 跑完，不代表 IRC 真的听上了。
        # 这里看一眼实际监听状态再返回，免得上层拿到一个假 True。
        self._started = True
        try:
            return bool(self.irc.running)
        except Exception:
            return False

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
            await self.dm_douyu.stop()
        except Exception:
            pass
        try:
            await self.dm_douyin.stop()
        except Exception:
            pass
        try:
            await self.irc.stop()
        except Exception:
            pass

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

    # ── 斗鱼 ──

    def _on_dm_state_douyu(self, state: str):
        self.dm_douyu_state = state

    def start_douyu(self, room) -> None:
        """房间号空着就是关掉，用户取消勾选时也走这里。"""
        room = str(room or "").strip()
        if not room:
            self.stop_douyu()
            return
        self.dm_douyu_room = room
        self.call(self.dm_douyu.start(room))

    def stop_douyu(self) -> None:
        self.dm_douyu_room = ""
        self.call(self.dm_douyu.stop())

    @property
    def douyu_connected(self) -> bool:
        return bool(self.dm_douyu.connected)

    # ── 抖音 ──

    def _on_dm_state_douyin(self, state: str):
        self.dm_douyin_state = state

    def start_douyin(self, room) -> None:
        room = str(room or "").strip()
        if not room:
            self.stop_douyin()
            return
        self.dm_douyin_room = room
        self.call(self.dm_douyin.start(room))

    def stop_douyin(self) -> None:
        self.dm_douyin_room = ""
        self.call(self.dm_douyin.stop())

    @property
    def douyin_connected(self) -> bool:
        return bool(self.dm_douyin.connected)

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
