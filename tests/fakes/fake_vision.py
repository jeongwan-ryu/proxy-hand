"""가짜 맵 비전 (맵 위 웹캠·맵 없이 관제를 시험). — 담당: 이보연

진짜 맵 비전(laptop/vision_service)과 같은 vision/state를 1초 10번 관제로 보낸다 (인터페이스 정의 5.3).
값은 웹캠 대신 콘솔에서 사람이 바꾼다. 영상(:8090)은 보내지 않는다.

실행 (저장소 맨 위 폴더에서):
    python -m tests.fakes.fake_vision

콘솔 명령:
    1 / 2 / 3     보관함 그 칸: 있음 ↔ 없음        (1 ?  → 모름 unknown)
    b             작업대: 있음 ↔ 없음              (b ?  → 모름)
    led           콕 LED: 켜짐 ↔ 꺼짐              (led none → LED를 못 봄, null)
    off / on      보내기 멈춤(관제가 "맵 비전 꺼짐"으로 봄) / 다시 보냄
    show          지금 보내는 값
    quit          끝
처음 값: 칸 1~3 있음, 작업대 비어 있음, LED 꺼짐.
"""

from __future__ import annotations

import argparse
import pathlib
import socket
import threading
from typing import Any, Dict, List, Optional, Tuple

from shared import config
from shared import messages as m

LOCAL_NETWORK = config.ROOT / "tests" / "fakes" / "network_local.yaml"
SEND_PERIOD_S = 0.1     # 진짜 맵 비전과 같이 1초 10번
FAKE_FPS = 30.0
HELP = "명령: 1|2|3 [?] | b [?] | led [none] | off | on | show | quit"
TOGGLE = {"occupied": "empty", "empty": "occupied", "unknown": "occupied"}
WORD = {"occupied": "있음", "empty": "없음", "unknown": "모름", True: "켜짐", False: "꺼짐", None: "못 봄"}


class FakeVision:
    def __init__(self, dest: Tuple[str, int]) -> None:
        self.dest = dest
        self.lock = threading.Lock()
        self.slots: Dict[str, str] = {k: "occupied" for k in m.SLOT_KEYS}
        self.bench = "empty"
        self.cock_led: Optional[bool] = False
        self.sending = True
        self.out = m.Sender()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()

    def message(self) -> Dict[str, Any]:
        with self.lock:
            return self.out.make(m.VISION_STATE, slots=dict(self.slots), bench=self.bench,
                                 cock_led=self.cock_led, fps=FAKE_FPS)

    def send_once(self) -> None:
        with self.lock:
            if not self.sending:
                return
        try:
            self.sock.sendto(m.encode(self.message()), self.dest)
        except OSError as e:          # 관제가 꺼져 있어도 계속 돈다
            print(f"[보내기 실패] {e}")

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(SEND_PERIOD_S):
                self.send_once()
        threading.Thread(target=loop, name="fake_vision", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self.sock.close()

    def show(self) -> str:
        with self.lock:
            slots = " ".join(f"칸{k}={WORD[v]}" for k, v in self.slots.items())
            return (f"{slots} 작업대={WORD[self.bench]} LED={WORD[self.cock_led]} "
                    f"({'보내는 중' if self.sending else '멈춤'})")


def handle_command(fv: FakeVision, line: str) -> bool:
    """콘솔 한 줄. False면 끝."""
    words = line.split()
    if not words:
        return True
    cmd, arg = words[0], (words[1] if len(words) > 1 else "")
    if cmd in ("quit", "exit", "q"):
        return False
    with fv.lock:
        if cmd in fv.slots:
            fv.slots[cmd] = "unknown" if arg == "?" else TOGGLE[fv.slots[cmd]]
        elif cmd == "b":
            fv.bench = "unknown" if arg == "?" else TOGGLE[fv.bench]
        elif cmd == "led":
            fv.cock_led = None if arg == "none" else not fv.cock_led
        elif cmd in ("off", "on"):
            fv.sending = cmd == "on"
        elif cmd != "show":
            print(HELP)
            return True
    print(fv.show())
    return True


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="가짜 맵 비전")
    ap.add_argument("--network", type=pathlib.Path, default=LOCAL_NETWORK, help="포트를 읽을 네트워크 설정")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    net = config.or_exit(config.load_network, args.network)
    fv = FakeVision(("127.0.0.1", net["laptop"]["vision_state_port"]))
    fv.start()
    print(f"가짜 맵 비전: vision/state → UDP {fv.dest[1]} (1초 10번)")
    print(fv.show())
    print(HELP)
    try:
        while True:
            if not handle_command(fv, input("> ")):
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        fv.close()
        print("가짜 맵 비전 끝")


if __name__ == "__main__":
    main()
