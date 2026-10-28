from __future__ import annotations

import socket
import threading
import time


class NetworkWatcher:
    """网络变化监视器。"""

    def __init__(self, interval: float = 30.0):
        self._interval = interval
        self._running = False
        self._thread: threading.Thread | None = None
        self._current_ip = ""
        self._listeners: list = []
        self._lock = threading.Lock()

    def get_lan_ip(self) -> str:
        """
        获取本机局域网 IP。

        优先用 netutil.socket_lan_ip()：它考虑了多网卡的优先级
        （Windows 上装了加速器会多出一张虚拟网卡，自己用 UDP connect 猜
        容易挑错卡，界面和 DNS 答复就会指向一个 PS5 连不到的地址）。
        """
        try:
            from . import netutil
            ip = netutil.socket_lan_ip()
            if ip:
                return ip
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return ""

    def on_change(self, callback) -> None:
        """注册 IP 变化回调：callback(old_ip, new_ip)"""
        with self._lock:
            self._listeners.append(callback)

    def start(self) -> None:
        if self._running:
            return
        self._current_ip = self.get_lan_ip()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="netwatch")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    @property
    def current_ip(self) -> str:
        return self._current_ip

    @property
    def is_running(self) -> bool:
        return self._running

    def _loop(self) -> None:
        while self._running:
            time.sleep(self._interval)
            try:
                new_ip = self.get_lan_ip()
                if new_ip and new_ip != self._current_ip:
                    old_ip = self._current_ip
                    self._current_ip = new_ip
                    for cb in self._listeners:
                        try:
                            cb(old_ip, new_ip)
                        except Exception:
                            pass
            except Exception:
                pass

    def check_now(self) -> bool:
        """立即检查一次，返回是否有变化。"""
        new_ip = self.get_lan_ip()
        if new_ip and new_ip != self._current_ip:
            old_ip = self._current_ip
            self._current_ip = new_ip
            for cb in self._listeners:
                try:
                    cb(old_ip, new_ip)
                except Exception:
                    pass
            return True
        return False
