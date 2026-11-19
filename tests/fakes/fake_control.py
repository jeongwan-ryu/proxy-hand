"""가짜 관제: 작업차 서비스의 TCP 5006에 접속해 goto·heartbeat 등을 보내고, 보고를 받는다.

    ctl = FakeControl("127.0.0.1", port, car=1)
    ctl.start_heartbeat()
    ctl.send(m.SET_STATION, station="wait_1")
    ctl.wait_for(m.ARRIVED)
    ctl.send_hand(udp_port, [90] * 6, 33)
"""

import socket
import threading
import time
from typing import Any, Dict, List, Optional

from shared import messages as m


class FakeControl:
    def __init__(self, host: str, port: int, car: int = 1) -> None:
        self.host, self.car = host, car
        self.sock = socket.create_connection((host, port), timeout=2)
        self.out = m.Sender()
        self.got: List[Dict[str, Any]] = []
        self._cond = threading.Condition()
        self._hb = False
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        threading.Thread(target=self._read, daemon=True).start()

    def send(self, name: str, **fields: Any) -> None:
        self.sock.sendall(m.encode(self.out.make(m.car_topic(self.car, name), **fields)))

    def send_raw(self, line: bytes) -> None:
        self.sock.sendall(line)

    def send_hand(self, udp_port: int, angles: List[int], ms: int) -> None:
        msg = self.out.make(m.car_topic(self.car, m.HAND), angles=angles, ms=ms)
        self._udp.sendto(m.encode(msg), (self.host, udp_port))

    def start_heartbeat(self, period: float = 0.2) -> None:
        self._hb = True

        def beat() -> None:
            while self._hb:
                try:
                    self.send(m.HEARTBEAT)
                except OSError:
                    return
                time.sleep(period)
        threading.Thread(target=beat, daemon=True).start()

    def stop_heartbeat(self) -> None:
        self._hb = False

    def wait_for(self, name: str, timeout: float = 3.0, **match: Any) -> Dict[str, Any]:
        """name(예: "arrived") 메시지 중 match 필드가 맞는 첫 것. 받은 것은 got에서 지움."""
        topic = m.car_topic(self.car, name)
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                for i, msg in enumerate(self.got):
                    if msg["topic"] == topic and all(msg.get(k) == v for k, v in match.items()):
                        return self.got.pop(i)
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"{topic} {match} 안 옴. 받은 것: {[x['topic'] for x in self.got]}")
                self._cond.wait(left)

    def last_status(self) -> Optional[Dict[str, Any]]:
        with self._cond:
            st = [x for x in self.got if x["topic"].endswith("/status")]
            return st[-1] if st else None

    def close(self) -> None:
        self._hb = False
        self.sock.close()
        self._udp.close()

    def _read(self) -> None:
        reader = m.LineReader()
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return
                for line in reader.feed(chunk):
                    with self._cond:
                        self.got.append(m.decode(line))
                        self._cond.notify_all()
        except OSError:
            return
