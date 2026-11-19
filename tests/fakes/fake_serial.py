"""가짜 micro:bit (SerialLink 대신). P, M에 OK로 답함. 응답 없음·ERR도 흉내.

    link = FakeSerial()
    link.mode = "silent"   # 응답 없음 (실패로 셈)
    link.mode = "err"      # ERR range
"""

import threading
from typing import List, Optional, Tuple

from shared import messages as m


class FakeSerial:
    def __init__(self, mode: str = "ok", can_open: bool = True) -> None:
        self.mode = mode
        self.can_open = can_open
        self.sent: List[bytes] = []
        self._open = False
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> bool:
        self._open = self.can_open
        return self._open

    def close(self) -> None:
        self._open = False

    def exchange(self, cmd: bytes) -> Optional[Tuple[bool, str]]:
        with self._lock:
            self.sent.append(cmd)
        if self.mode == "silent":
            return None
        reply = b"ERR range" if self.mode == "err" and cmd.startswith(b"M") else b"OK"
        return m.parse_serial_reply(reply)

    def moves(self) -> List[bytes]:
        with self._lock:
            return [c for c in self.sent if c.startswith(b"M")]
