from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime


class DanmakuHistory:
    """弹幕历史记录器。"""

    def __init__(self, data_dir: str = "data/danmaku", max_memory: int = 500):
        self._data_dir = data_dir
        self._max_memory = max_memory
        self._recent: deque = deque(maxlen=max_memory)
        self._lock = threading.Lock()
        self._file = None
        self._file_path = ""
        os.makedirs(data_dir, exist_ok=True)

    def start_session(self, room_id: int = 0) -> str:
        """开始一场直播的弹幕记录。返回文件路径。"""
        self.close_session()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = "danmaku_%s_%d.jsonl" % (ts, room_id or 0)
        self._file_path = os.path.join(self._data_dir, name)
        self._file = open(self._file_path, "a", encoding="utf-8")
        return self._file_path

    def close_session(self) -> None:
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None

    def append(self, kind: str, user: str, text: str, extra: dict = None) -> None:
        """记录一条弹幕。"""
        record = {
            "ts": time.time(),
            "kind": kind,
            "user": user,
            "text": text,
        }
        if extra:
            record.update(extra)
        with self._lock:
            self._recent.append(record)
            if self._file:
                try:
                    self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    self._file.flush()
                except Exception:
                    pass

    def recent(self, count: int = 50) -> list:
        """获取最近 N 条弹幕。"""
        with self._lock:
            return list(self._recent)[-count:]

    def search(self, keyword: str, count: int = 20) -> list:
        """搜索弹幕。"""
        result = []
        with self._lock:
            for r in reversed(self._recent):
                if keyword in r.get("user", "") or keyword in r.get("text", ""):
                    result.append(r)
                    if len(result) >= count:
                        break
        return result

    @property
    def current_file(self) -> str:
        return self._file_path

    @property
    def is_recording(self) -> bool:
        return self._file is not None

    def list_sessions(self) -> list:
        """列出所有弹幕记录文件。"""
        files = []
        try:
            for f in sorted(os.listdir(self._data_dir)):
                if f.endswith(".jsonl"):
                    path = os.path.join(self._data_dir, f)
                    files.append({
                        "name": f,
                        "path": path,
                        "size": os.path.getsize(path),
                        "modified": os.path.getmtime(path),
                    })
        except Exception:
            pass
        return files
