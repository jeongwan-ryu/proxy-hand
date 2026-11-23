"""가짜 작업차 2대 (파이·Raspbot·맵 없이 관제를 시험). — 담당: 이보연

정완 님의 진짜 작업차 서비스(pi/car_service의 CarCore + Service)를 그대로 돌리고,
바퀴·선 센서 자리에만 가짜 선로(FakeTrack)를 끼운다. micro:bit는 가짜(FakeSerial) 또는 진짜(--serial COM4).

    가짜 선로: 고리 하나 = 정지선 n개를 같은 간격으로 둔 원
      바퀴 값 → 위치가 앞으로 감 (보통 속도로 정지선 사이 --segment초)
      위치가 정지선 위 → 센서 1111 (검정 4개), 그 밖 → 0110 (가운데 선)
      위치 지정(set_station) → 차를 그 정지선 위에 옮겨 놓음 (사람이 차를 놓는 것)

실행 (저장소 맨 위 폴더에서, 관제는 --network tests/fakes/network_local.yaml로 따로 실행):
    python -m tests.fakes.fake_car
    python -m tests.fakes.fake_car --serial COM4      # 작업차 1의 손 명령을 진짜 AiHand로
    python -m tests.fakes.fake_car --segment 1        # 정지선 사이 1초 (빨리)

콘솔 명령 (N = 작업차 번호 1 또는 2):
    where            두 차의 상태·위치
    lost N           선을 잃게 함 → line_lost
    miss N           다음 정지선을 못 보게 함 → line_missed
    serial N off|on  micro:bit 응답 없음 → serial_fail / 다시 응답 (가짜 micro:bit만)
    drop N           관제와의 TCP 연결을 끊음 (관제가 다시 접속함)
    quit             끝
"""

from __future__ import annotations

import argparse
import copy
import pathlib
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from pi.car_service.main import CarCore, Service
from shared import config
from shared import messages as m
from tests.fakes.fake_line import CENTER, NOTHING, STOPLINE
from tests.fakes.fake_serial import FakeSerial

LOCAL_NETWORK = config.ROOT / "tests" / "fakes" / "network_local.yaml"
SPEED_NORMAL, SPEED_SLOW, TURN_SMALL, TURN_BIG = 100, 50, 20, 60   # 가짜 바퀴 값 (car.yaml은 아직 0)
STOPLINE_S = 0.1          # 보통 속도로 정지선 하나를 지나는 시간
LOST_S = 1.0              # lost: 이만큼 선이 안 보임 (line_lost_search_s 0.5보다 길게)


class FakeTrack:
    """RaspbotHW 대신. 위치 pos는 '정지선 간격 = 1'인 단위, 정지선 k는 [k, k + width) 구간."""

    def __init__(self, lines: int, segment_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.n = lines
        self.v_normal = 1.0 / segment_s            # 보통 속도일 때 1초에 가는 거리
        self.width = STOPLINE_S * self.v_normal
        self.clock = clock
        self.lock = threading.Lock()
        self.pos = self.width / 2                  # 처음엔 대기 정거장(0번) 정지선 위
        self.speed = 0.0
        self._last_t: Optional[float] = None
        self.lost_until = 0.0
        self.hide_next = False                     # miss: 다음에 만나는 정지선을 안 보이게
        self.hidden: Optional[int] = None
        self._prev: Optional[int] = 0              # 지난번 읽을 때 밑에 있던 정지선

    # --- RaspbotHW와 같은 함수 ---
    def drive(self, left: float, right: float) -> None:
        with self.lock:
            self._advance()
            self.speed = (left + right) / 2 / SPEED_NORMAL * self.v_normal

    def stop(self) -> None:
        self.drive(0, 0)

    def read_sensors(self) -> Tuple[int, int, int, int]:
        with self.lock:
            self._advance()
            line = self._line()
            entered = line is not None and line != self._prev
            self._prev = line
            if line is None:
                self.hidden = None
            elif entered and self.hide_next:       # 서 있던 정지선이 아니라 새로 들어가는 정지선만 숨김
                self.hide_next, self.hidden = False, line
            if self.clock() < self.lost_until:
                return NOTHING
            if line is None or line == self.hidden:
                return CENTER
            return STOPLINE

    # --- 가짜 선로 조작 ---
    def place(self, line: int) -> None:
        """그 정지선 위에 차를 놓음."""
        with self.lock:
            self._advance()
            self.pos = line + self.width / 2
            self.hidden, self._prev = None, line

    def lose_line(self) -> None:
        with self.lock:
            self.lost_until = self.clock() + LOST_S

    def miss_next(self) -> None:
        with self.lock:
            self.hide_next = True

    def line_under(self) -> Optional[int]:
        """지금 밑에 있는 정지선 번호 (없으면 None)."""
        with self.lock:
            self._advance()
            return self._line()

    def _line(self) -> Optional[int]:
        k = int(self.pos)
        return k % self.n if self.pos - k < self.width else None

    def _advance(self) -> None:
        now = self.clock()
        if self._last_t is not None:
            self.pos = (self.pos + self.speed * (now - self._last_t)) % self.n
        self._last_t = now


class FakeCarCore(CarCore):
    """진짜 CarCore + 위치 지정 때 가짜 선로 위 차도 그 정지선으로 옮김."""

    def __init__(self, track: FakeTrack, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.track = track

    def _on_set_station(self, msg: Dict[str, Any], now: float) -> None:
        super()._on_set_station(msg, now)
        self.track.place(self.my_stations[msg["station"]]["line"])


def car_config(car_id: int, sensor_hz: float) -> Dict[str, Any]:
    return dict(config.load_car(require_tuned=False), car_id=car_id, invert_sensors=False, sensor_hz=sensor_hz,
                speed_normal=SPEED_NORMAL, speed_slow=SPEED_SLOW, turn_small=TURN_SMALL, turn_big=TURN_BIG)


def map_config(segment_s: float) -> config.MapConfig:
    """map.yaml + 구간 시간(가짜 선로에 맞춤). segment_s가 있어야 정지선 놓침 확인(line_missed)이 켜짐."""
    raw = copy.deepcopy(config.load_map().raw)
    for s in raw["stations"]:
        s["segment_s"] = round(segment_s - STOPLINE_S, 3)
    return config.MapConfig(raw)


def console_log(car_id: int) -> Callable[..., None]:
    def log(type_: str, **f: Any) -> None:
        if type_ == "state_change":
            print(f"  [작업차 {car_id}] {f['from']} → {f['to']} ({f['cause']})")
        elif type_ in ("error", "link"):
            print(f"  [작업차 {car_id}] {type_}: {f.get('code', '')} {f.get('text', '')}".rstrip())
    return log


class FakeCar:
    """가짜 작업차 하나: 진짜 Service + 가짜 선로 + 가짜/진짜 micro:bit."""

    def __init__(self, car_id: int, mp: config.MapConfig, timing: Dict[str, float], cmd_port: int, hand_port: int,
                 segment_s: float = 2.0, serial_link: Any = None, sensor_hz: float = 50,
                 log: Optional[Callable[..., None]] = None) -> None:
        self.car_id = car_id
        car = car_config(car_id, sensor_hz)
        loop = mp.cars[str(car_id)]["loop"]
        self.track = FakeTrack(mp.loops[loop], segment_s)
        self.serial = serial_link if serial_link is not None else FakeSerial()
        sender = m.Sender()
        self.core = FakeCarCore(self.track, car, mp, timing, sender, log or (lambda type_, **f: None))
        self.service = Service(self.core, self.track, self.serial, cmd_port, hand_port, sensor_hz, host="127.0.0.1")

    @property
    def ports(self) -> Tuple[int, int]:
        return self.service.ports

    def start(self) -> None:
        self.service.start()
        threading.Thread(target=self.service.run, name=f"car{self.car_id}", daemon=True).start()

    def close(self) -> None:
        self.service.close()

    # --- 콘솔 명령 ---
    def set_serial(self, on: bool) -> bool:
        if not isinstance(self.serial, FakeSerial):
            return False
        self.serial.mode = "ok" if on else "silent"
        return True

    def drop(self) -> bool:
        with self.service._send_lock:
            client = self.service._client
        if client is None:
            return False
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        return True

    def where(self) -> str:
        with self.service.lock:
            c = self.core
            state, station, moving_to, serial_ok = c.state, c.station, c.moving_to, c.serial_ok
            pos_line = c.tracker.pos_line
        with self.track.lock:
            pos = self.track.pos
        return (f"작업차 {self.car_id}: {state:8} 정거장={station} 가는 곳={moving_to} "
                f"지난 정지선={pos_line} 선로 위치={pos:.2f} micro:bit={'OK' if serial_ok else '안 됨'}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="가짜 작업차 2대")
    ap.add_argument("--network", type=pathlib.Path, default=LOCAL_NETWORK, help="포트를 읽을 네트워크 설정")
    ap.add_argument("--serial", help="작업차 1의 micro:bit 포트 (예: COM4). 없으면 가짜 micro:bit")
    ap.add_argument("--segment", type=float, default=2.0, help="보통 속도로 정지선 사이를 가는 시간(초)")
    args = ap.parse_args(argv)
    if args.segment <= STOPLINE_S * 2:
        ap.error(f"--segment는 {STOPLINE_S * 2}초보다 커야 함")
    return args


def handle_command(cars: Dict[int, FakeCar], line: str) -> bool:
    """콘솔 한 줄. False면 끝."""
    words = line.split()
    if not words:
        return True
    cmd = words[0]
    if cmd in ("quit", "exit", "q"):
        return False
    if cmd == "where":
        for car in cars.values():
            print(car.where())
        return True
    car = cars.get(int(words[1])) if len(words) > 1 and words[1].isdigit() else None
    if car is None or cmd not in ("lost", "miss", "serial", "drop"):
        print("명령: where | lost N | miss N | serial N off|on | drop N | quit")
        return True
    if cmd == "lost":
        car.track.lose_line()
        print(f"작업차 {car.car_id}: {LOST_S}초 동안 선이 안 보임 (달리는 중이면 line_lost)")
    elif cmd == "miss":
        car.track.miss_next()
        print(f"작업차 {car.car_id}: 다음 정지선을 못 봄 (달리는 중이면 line_missed)")
    elif cmd == "serial":
        on = len(words) > 2 and words[2] == "on"
        if len(words) < 3 or words[2] not in ("on", "off"):
            print("serial N off 또는 serial N on")
        elif not car.set_serial(on):
            print("진짜 micro:bit(--serial)는 콘솔로 끌 수 없음. USB를 빼서 시험하세요.")
        else:
            print(f"작업차 {car.car_id}: micro:bit {'응답함' if on else '응답 없음'}")
    elif cmd == "drop":
        print(f"작업차 {car.car_id}: " + ("관제 연결을 끊음" if car.drop() else "관제가 접속해 있지 않음"))
    return True


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    net = config.or_exit(config.load_network, args.network)
    mp = map_config(args.segment)
    cars: Dict[int, FakeCar] = {}
    for n in m.CAR_IDS:
        port = net["cars"][str(n)]
        link = None
        if n == 1 and args.serial:
            from pi.car_service.hardware import SerialLink
            c = car_config(n, 50)
            link = SerialLink(args.serial, c["serial_baud"], c["serial_timeout_s"])
        cars[n] = FakeCar(n, mp, net["timing"], port["cmd_port"], port["hand_port"], args.segment, link,
                          log=console_log(n))
        print(f"가짜 작업차 {n}: 명령 TCP {port['cmd_port']}, 손 UDP {port['hand_port']}, "
              f"micro:bit {args.serial if link else '가짜'}")
    for car in cars.values():
        car.start()
    print(f"정지선 사이 {args.segment}초. 관제에서 [위치 지정]을 하면 차가 그 정지선 위에 놓입니다.")
    print("명령: where | lost N | miss N | serial N off|on | drop N | quit")
    try:
        while True:
            if not handle_command(cars, input("> ")):
                break
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        for car in cars.values():
            car.close()
        print("가짜 작업차 끝")


if __name__ == "__main__":
    main()
