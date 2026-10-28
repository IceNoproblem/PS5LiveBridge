from __future__ import annotations

import json
import os
import threading
import time


class ConfigStore:
    """细粒度配置存储。"""

    def __init__(self, data_dir: str = "data"):
        self._data_dir = data_dir
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._dirty: set = set()
        os.makedirs(data_dir, exist_ok=True)

    def _path(self, section: str) -> str:
        return os.path.join(self._data_dir, "%s.json" % section)

    def load(self, section: str) -> dict:
        """加载配置段（带缓存）。"""
        with self._lock:
            if section in self._cache:
                return self._cache[section]
        try:
            with open(self._path(section), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        with self._lock:
            self._cache[section] = data
        return data

    def save(self, section: str, data: dict) -> bool:
        """保存配置段。"""
        with self._lock:
            self._cache[section] = data
            self._dirty.add(section)
        return self._flush(section)

    def update(self, section: str, **kwargs) -> bool:
        """增量更新配置段。"""
        data = self.load(section)
        data.update(kwargs)
        return self.save(section, data)

    def get(self, section: str, key: str, default=None):
        """获取单个配置项。"""
        return self.load(section).get(key, default)

    def set(self, section: str, key: str, value) -> bool:
        """设置单个配置项。"""
        data = self.load(section)
        data[key] = value
        return self.save(section, data)

    def _flush(self, section: str) -> bool:
        """写入磁盘。"""
        with self._lock:
            data = self._cache.get(section, {})
            self._dirty.discard(section)
        try:
            path = self._path(section)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    def flush_all(self) -> None:
        """写入所有脏配置。"""
        with self._lock:
            sections = list(self._dirty)
        for s in sections:
            self._flush(s)

    def save_auth(self, cookie: str = "", room_id: int = 0, uid: int = 0) -> None:
        self.update("auth", cookie=cookie, room_id=room_id, uid=uid,
                    updated=time.time())

    def load_auth(self) -> dict:
        return self.load("auth")

    def save_live(self, area_id: str = "", title: str = "",
                  auto_start: bool = True, auto_relay: bool = True) -> None:
        self.update("live", area_id=area_id, title=title,
                    auto_start=auto_start, auto_relay=auto_relay)

    def load_live(self) -> dict:
        return self.load("live")

    def save_network(self, ip: str = "", dns: str = "",
                     gateway: str = "", rtmp_port: int = 1935) -> None:
        self.update("network", ip=ip, dns=dns, gateway=gateway, rtmp_port=rtmp_port)

    def load_network(self) -> dict:
        return self.load("network")

    def list_sections(self) -> list:
        """列出所有配置段。"""
        try:
            return [f[:-5] for f in os.listdir(self._data_dir) if f.endswith(".json")]
        except Exception:
            return []

    def export_all(self) -> dict:
        """导出所有配置（用于备份）。"""
        result = {}
        for section in self.list_sections():
            result[section] = self.load(section)
        return result

    def import_all(self, data: dict) -> None:
        """导入配置（用于恢复）。"""
        for section, values in data.items():
            if isinstance(values, dict):
                self.save(section, values)
