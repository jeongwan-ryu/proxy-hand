"""
노트북 로그 (시스템 설계 14장, 인터페이스 정의 9.8).

관제 서비스를 켤 때마다 data/logs/<YYYY-MM-DD_HHMMSS>/ 폴더를 새로 만들고
events.jsonl(모드·명령·보고·작업·작업대·알림·화면 입력·결과)과 hand.jsonl(보낸 손 각도 전부)을 쓴다.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import time
from typing import Any, Callable, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOGS_DIR = ROOT / "data" / "logs"
HAND_FLUSH_S = 1.0      # hand.jsonl은 1초 30줄이라 1초마다 한 번에 씀


class Logbook:
    def __init__(self, folder: Optional[pathlib.Path] = None, clock: Callable[[], float] = time.time) -> None:
        if folder is None:
            folder = LOGS_DIR / datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        self.clock = clock
        self._events = open(folder / "events.jsonl", "a", encoding="utf-8")
        self._hand = open(folder / "hand.jsonl", "a", encoding="utf-8")
        self._hand_last_flush = 0.0

    def event(self, type_: str, **fields: Any) -> None:
        row = {"t": round(self.clock(), 3), "type": type_}
        row.update(fields)
        self._events.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._events.flush()

    def hand(self, car: int, angles: List[int], ms: int, src: str) -> None:
        row = {"t": round(self.clock(), 3), "type": "hand", "car": car, "angles": angles, "ms": ms, "src": src}
        self._hand.write(json.dumps(row) + "\n")
        now = time.monotonic()
        if now - self._hand_last_flush >= HAND_FLUSH_S:
            self._hand.flush()
            self._hand_last_flush = now

    def close(self) -> None:
        for f in (self._events, self._hand):
            try:
                f.flush()
                f.close()
            except OSError:
                pass


class MemoryLog:
    """시험용: 파일 대신 목록에 쌓음."""

    def __init__(self) -> None:
        self.events: List[dict] = []
        self.hands: List[Tuple[int, List[int], int, str]] = []

    def event(self, type_: str, **fields: Any) -> None:
        row = {"type": type_}
        row.update(fields)
        self.events.append(row)

    def hand(self, car: int, angles: List[int], ms: int, src: str) -> None:
        self.hands.append((car, list(angles), ms, src))

    def of(self, type_: str) -> List[dict]:
        return [e for e in self.events if e["type"] == type_]

    def close(self) -> None:
        pass
