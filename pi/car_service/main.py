"""
작업차 서비스 (각 파이) — 담당: 유정완

하는 일: 선 따라가기, 정지선 세기, 정지선 놓침 확인(구간 시간, line_missed),
        손 명령을 micro:bit로 전달(arrived일 때만), 생존 신호 끊기면 안전 멈춤.
입력: car{n}/hand (UDP 5005), 관제 명령 (TCP 5006, 파이가 기다림), 선 센서, micro:bit 응답
출력: 바퀴 구동, 시리얼 P/M 명령, car{n}/status·arrived·error·reject
관련 SR: SR-05, SR-08, SR-10~13, SR-36, SR-38
참고: 시스템 설계 6.2·10장, 인터페이스 정의 4·7장

구성:
    LineTracker  센서 4개 → 바퀴 값, 정지선 세기, 선 놓침·정지선 놓침 (시스템 설계 10장). 하드웨어 모름.
    CarCore      파이 상태(4.4), 명령 처리, 생존 신호, 손 명령 최신값, 시리얼 실패 세기. 하드웨어·네트워크 모름.
    Service      스레드 3개(TCP 명령, UDP 손, 시리얼) + 센서 주기 루프로 CarCore를 돌림.
시각: 판단은 time.monotonic(), 메시지 t와 로그는 time_sync 차이를 더한 시각 (Sender.now()).

실행 (저장소 맨 위에서):  python -m pi.car_service.main
파이의 Python 버전에 맞추기 위해 3.9 문법까지만 쓴다.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared import config
from shared import messages as m

Motors = Tuple[float, float]
STOP: Motors = (0.0, 0.0)
SERIAL_RETRY_S = 1.0      # serial_fail 뒤 다시 연결 시도 간격 (시스템 설계 11장)
SERIAL_IDLE_PING_S = 2.0  # 손 명령이 없을 때 P로 연결 확인하는 간격 (status의 serial_ok가 맞게)


# ---------------------------------------------------------------------------
# 선 따라가기 + 정지선 세기 (시스템 설계 10장)
# ---------------------------------------------------------------------------

class LineTracker:
    """위치는 '마지막으로 지난 정지선 번호(pos_line)'로 기억한다. 대기 정거장 = 0번.
    정거장 사이에서 멈췄다가 다시 가도 이어서 센다 (인터페이스 정의 4.4)."""

    def __init__(self, car: Dict[str, Any], loop_lines: int, segment_s: Dict[int, float],
                 line_check: Optional[Dict[str, float]]) -> None:
        self.c = car
        self.n = loop_lines
        self.segment_s = segment_s           # 정지선 번호 → 앞 정지선 끝부터 이 정지선 시작까지 기준 시간
        self.line_check = line_check         # None이면 정지선 놓침 확인 끔 (segment_s가 0인 정거장이 있음)
        self.pos_line: Optional[int] = None  # 모르면 None (켜진 뒤 set_station 전)
        self.target: Optional[int] = None
        self.on_line = False                 # 지금 정지선 위
        self.skip_end = False                # 출발한 정지선: 끝나도 세지 않음 (10.2)
        self._start_reads = self._end_reads = 0
        self.seg_start: Optional[float] = None
        self._lost_since: Optional[float] = None
        self._last: Motors = STOP

    def set_line(self, line: int) -> None:
        """정거장(정지선 위)에 서 있다고 둠: set_station, 도착."""
        self.pos_line, self.on_line, self.skip_end = line, True, True
        self._start_reads = self._end_reads = 0
        self.seg_start = None

    def start(self, target: int, now: float) -> None:
        """target 정지선으로 출발. 멈췄다 다시 가는 구간은 시간을 처음부터 잼 (10.3)."""
        self.target = target
        self._lost_since = None
        self._last = STOP
        self.seg_start = None if self.on_line else now

    @property
    def next_line(self) -> int:
        return (self.pos_line + 1) % self.n  # type: ignore[operator]

    @property
    def slow(self) -> bool:
        """목표 바로 앞 정지선을 지난 뒤(= 다음 정지선이 목표)부터 느린 속도 (10.2)."""
        return self.next_line == self.target

    def step(self, s: Tuple[int, int, int, int], now: float) -> Tuple[Motors, Optional[str]]:
        """센서 (L2, L1, R1, R2), 검정=1 → (바퀴 값, 일). 일: None / "arrived" / "line_lost" / "line_missed"."""
        c = self.c
        black = sum(s)
        if black >= c["stopline_min_black"]:
            self._end_reads = 0
            self._lost_since = None
            if not self.on_line:
                self._start_reads += 1
                if self._start_reads >= c["stopline_confirm_reads"]:      # 정지선 시작
                    self._start_reads = 0
                    if self.next_line == self.target:
                        self.set_line(self.target)
                        return STOP, "arrived"                            # 시작을 보는 즉시 멈춤
                    self.on_line, self.skip_end, self.seg_start = True, False, None
            base = self._base()
            return self._remember((base, base)), None                     # 정지선 위는 곧게

        self._start_reads = 0
        if self.on_line:
            self._end_reads += 1
            if self._end_reads >= c["stopline_confirm_reads"]:            # 정지선 끝
                self._end_reads = 0
                self.on_line = False
                if not self.skip_end:
                    self.pos_line = self.next_line
                self.skip_end = False
                self.seg_start = now

        if self.line_check and self.seg_start is not None:
            factor = self.line_check["slow_factor" if self.slow else "normal_factor"]
            if now - self.seg_start > self.segment_s[self.next_line] * factor:
                return STOP, "line_missed"

        if black == 0:                                                    # 선 없음: 하던 대로 돌며 찾음
            if self._lost_since is None:
                self._lost_since = now
            if now - self._lost_since > c["line_lost_search_s"]:
                return STOP, "line_lost"
            return self._last, None
        self._lost_since = None
        return self._remember(self._follow(s)), None

    def _base(self) -> float:
        return self.c["speed_slow"] if self.slow else self.c["speed_normal"]

    def _follow(self, s: Tuple[int, int, int, int]) -> Motors:
        """10.1 표. 선이 왼쪽에 있으면 왼쪽 바퀴를 느리게 (왼쪽으로 돎)."""
        base, small, big = self._base(), self.c["turn_small"], self.c["turn_big"]
        l2, l1, r1, r2 = s
        if (l2, l1, r1, r2) == (0, 1, 1, 0):
            return base, base
        if (l2, l1, r1, r2) == (0, 1, 0, 0):
            return max(0.0, base - small), base
        if (l2, l1, r1, r2) == (0, 0, 1, 0):
            return base, max(0.0, base - small)
        if l2 and not r1 and not r2:                       # 1100, 1000
            return max(0.0, base - big), base
        if r2 and not l1 and not l2:                       # 0011, 0001
            return base, max(0.0, base - big)
        return self._last if self._last != STOP else (base, base)   # 1001 같은 이상한 값: 하던 대로

    def _remember(self, cmd: Motors) -> Motors:
        self._last = cmd
        return cmd


# ---------------------------------------------------------------------------
# 파이 상태와 명령 (인터페이스 정의 4장)
# ---------------------------------------------------------------------------

class CarCore:
    """스레드에서 부를 때는 Service가 lock을 잡고 부른다. 보낼 메시지는 outbox에 쌓는다."""

    def __init__(self, car: Dict[str, Any], mp: config.MapConfig, timing: Dict[str, float],
                 sender: m.Sender, log: Callable[..., None] = lambda type_, **f: None) -> None:
        self.car_id = car["car_id"]
        self.c = car
        self.mp = mp
        self.timing = timing
        self.out = sender
        self.log = log
        loop = mp.cars[str(self.car_id)]["loop"]
        self.my_stations = {sid: s for sid, s in mp.stations.items() if s["loop"] == loop}
        segment = {s["line"]: float(s["segment_s"]) for s in self.my_stations.values()}
        check = mp.line_check if mp.line_check_enabled() else None
        self.line_check_on = check is not None
        self.tracker = LineTracker(car, mp.loops[loop], segment, check)

        self.state = "idle"
        self.station: Optional[str] = None     # 마지막으로 도착한 정거장
        self.moving_to: Optional[str] = None
        self.at_station = False                # station 정지선 위에 서 있고 그 뒤 안 움직임
        self.resume_ok = False                 # stopped 직전이 arrived였고 그 뒤 안 움직임 (6.2)
        self.last_hb: Optional[float] = None
        self.link_lost_pending = False
        self.serial_fails = 0
        self.serial_ok = False
        self.hand: Optional[Tuple[List[int], int]] = None   # 가장 최근 손 명령만 (9.4)
        self.outbox: List[Dict[str, Any]] = []
        self._next_status = 0.0

    # --- 보내기 ----------------------------------------------------------------

    def _send(self, name: str, **fields: Any) -> None:
        self.outbox.append(self.out.make(m.car_topic(self.car_id, name), **fields))

    def status_msg(self) -> None:
        self._send(m.STATUS, state=self.state, station=self.station,
                   moving_to=self.moving_to, serial_ok=self.serial_ok)

    def take_outbox(self) -> List[Dict[str, Any]]:
        out, self.outbox = self.outbox, []
        return out

    def _set_state(self, new: str, cause: str) -> None:
        if new == self.state:
            return
        self.log("state_change", **{"from": self.state, "to": new, "cause": cause})
        if new == "stopped":
            self.resume_ok = self.state == "arrived"
        if new != "arrived":
            self.hand = None                   # arrived가 아니면 손 명령을 넘기지 않음
        if new != "driving":
            self.moving_to = None
        self.state = new
        self.status_msg()

    def _error(self, code: str, text: str) -> None:
        self.log("error", code=code, text=text)
        self._send(m.ERROR, code=code, text=text)
        self._set_state("error", code)

    def _arrive(self, station: str, cause: str) -> None:
        self.station, self.at_station = station, True
        self._set_state("arrived", cause)
        self._send(m.ARRIVED, station=station)

    # --- 관제 연결 ---------------------------------------------------------------

    def connected(self) -> None:
        """관제가 TCP로 접속한 직후 status (4.2)."""
        self.status_msg()

    def handle(self, msg: Dict[str, Any], now: float) -> None:
        """검사를 통과한 관제 메시지 하나."""
        car, name = m.split_car_topic(msg["topic"])  # type: ignore[misc]
        if name != m.HAND and name != m.HEARTBEAT:
            self.log("cmd", topic=msg["topic"], fields={k: v for k, v in msg.items() if k not in ("topic", "seq", "t")})
        if name not in m.TO_PI:
            return                                       # 파이가 받는 메시지가 아님 (status 등): 무시
        if car != self.car_id:
            return self.reject(msg["topic"], f"작업차 {self.car_id}인데 작업차 {car} 명령")
        policy = m.pi_policy(name, self.state)
        if policy == m.DROP:
            return
        if policy == m.REJECT_CMD:
            return self.reject(msg["topic"], f"{self.state} 상태에서는 {name}을 받지 않음")
        if "station" in msg and msg["station"] not in self.my_stations:
            return self.reject(msg["topic"], f"이 작업차 고리에 없는 정거장: {msg['station']}")
        getattr(self, "_on_" + name)(msg, now)

    def reject(self, topic: str, text: str) -> None:
        self.log("reject", cmd=topic, text=text)
        self._send(m.REJECT, cmd=topic, state=self.state, text=text)

    def _on_hand(self, msg: Dict[str, Any], now: float) -> None:
        self.hand = (msg["angles"], msg["ms"])

    def _on_goto(self, msg: Dict[str, Any], now: float) -> None:
        target = msg["station"]
        if self.at_station and target == self.station:
            self._arrive(target, "goto_same")              # 움직이지 않고 바로 arrived (4.4)
            return
        if self.tracker.pos_line is None:                  # 4.4 표상 생기지 않지만 위치를 모르면 못 감
            return self.reject(msg["topic"], "위치를 모름. 위치 지정(set_station) 먼저")
        self.at_station = False
        self.tracker.start(self.my_stations[target]["line"], now)
        self.moving_to = target
        self._set_state("driving", "goto")

    def _on_set_station(self, msg: Dict[str, Any], now: float) -> None:
        self.tracker.set_line(self.my_stations[msg["station"]]["line"])
        self._arrive(msg["station"], "set_station")

    def _on_stop(self, msg: Dict[str, Any], now: float) -> None:
        self._set_state("stopped", "stop:" + msg.get("reason", ""))

    def _on_resume(self, msg: Dict[str, Any], now: float) -> None:
        if self.resume_ok and self.at_station:
            self._set_state("arrived", "resume")
        else:
            self.status_msg()                              # stopped 그대로, goto만 받음

    def _on_heartbeat(self, msg: Dict[str, Any], now: float) -> None:
        self.last_hb = now
        if self.link_lost_pending:                         # 연결이 돌아온 뒤 알림 (4.2)
            self.link_lost_pending = False
            self._send(m.ERROR, code="link_lost",
                       text=f"생존 신호가 {self.timing['link_timeout_s']}초 넘게 끊겨 멈췄었음")

    def _on_time_sync(self, msg: Dict[str, Any], now: float) -> None:
        self.out.apply_time_sync(msg["now"])
        self.log("time_sync", offset=round(self.out.time_offset, 3))

    # --- 주기 처리 ---------------------------------------------------------------

    def tick(self, sensors: Optional[Tuple[int, int, int, int]], now: float) -> Motors:
        """센서 주기마다. sensors는 핀 값 그대로 (driving일 때만 읽어 넘김). 바퀴 값을 돌려줌."""
        if self.last_hb is not None and now - self.last_hb > self.timing["link_timeout_s"]:
            self.last_hb = None                            # 다음 heartbeat까지 다시 세지 않음
            self.link_lost_pending = True
            self.log("error", code="link_lost", text="생존 신호 끊김")
            self._set_state("stopped", "link_lost")        # 연결이 돌아와도 스스로 움직이지 않음

        motors = STOP
        if self.state == "driving" and sensors is not None:
            if self.c["invert_sensors"]:
                sensors = tuple(1 - v for v in sensors)    # type: ignore[assignment]
            motors, event = self.tracker.step(sensors, now)  # type: ignore[arg-type]
            if event == "arrived":
                self._arrive(self.moving_to, "arrived")    # type: ignore[arg-type]
            elif event == "line_lost":
                self._error("line_lost", f"선을 {self.c['line_lost_search_s']}초 동안 찾지 못함")
            elif event == "line_missed":
                self._error("line_missed", f"정지선 {self.tracker.next_line}번까지 기준 시간보다 오래 걸림")

        if now >= self._next_status:
            self._next_status = now + self.timing["status_period_s"]
            self.status_msg()
        return motors if self.state == "driving" else STOP

    # --- 시리얼 (Service의 시리얼 스레드가 부름) ------------------------------------

    def take_hand(self) -> Optional[Tuple[List[int], int]]:
        if self.state != "arrived":
            return None
        job, self.hand = self.hand, None
        return job

    def serial_result(self, ok: bool, detail: str = "") -> None:
        if ok:
            if not self.serial_ok:
                self.log("serial", text="micro:bit 연결됨")
            self.serial_fails, self.serial_ok = 0, True
            return
        if self.serial_fails >= self.c["serial_max_fail"]:
            return                             # 이미 serial_fail. 다시 연결될 때까지 세지 않음
        self.serial_fails += 1
        self.log("serial", text=f"응답 없음 {self.serial_fails}번 {detail}".strip())
        if self.serial_fails == self.c["serial_max_fail"]:
            self.serial_ok = False
            self._error("serial_fail", f"micro:bit가 {self.c['serial_timeout_s']}초 안에 응답하지 않음 "
                                       f"{self.serial_fails}번")


# ---------------------------------------------------------------------------
# 로그 (인터페이스 정의 9.8, 파이: ~/proxyhand/logs/<날짜>.jsonl)
# ---------------------------------------------------------------------------

class PiLog:
    def __init__(self, log_dir: str, clock: Callable[[], float]) -> None:
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, datetime.date.today().isoformat() + ".jsonl")
        self.clock = clock
        self._lock = threading.Lock()

    def __call__(self, type_: str, **fields: Any) -> None:
        line = json.dumps({"t": round(self.clock(), 3), "type": type_, **fields}, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# 스레드와 네트워크
# ---------------------------------------------------------------------------

class Service:
    def __init__(self, core: CarCore, hw: Any, serial_link: Any, cmd_port: int, hand_port: int,
                 sensor_hz: float, host: str = "0.0.0.0") -> None:
        self.core, self.hw, self.serial = core, hw, serial_link
        self.lock = threading.Lock()
        self.period = 1.0 / sensor_hz
        self.running = True
        self._client: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind((host, cmd_port))
        self.tcp.listen(1)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((host, hand_port))
        self.udp.settimeout(0.5)

    @property
    def ports(self) -> Tuple[int, int]:
        return self.tcp.getsockname()[1], self.udp.getsockname()[1]

    def start(self) -> None:
        for target, name in ((self._tcp_loop, "tcp"), (self._udp_loop, "udp"), (self._serial_loop, "serial")):
            threading.Thread(target=target, name=name, daemon=True).start()

    def close(self) -> None:
        self.running = False
        for s in (self.tcp, self.udp, self._client):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    def _flush(self) -> None:
        with self.lock:
            msgs = self.core.take_outbox()
        if not msgs:
            return
        with self._send_lock:
            client = self._client
            if client is None:
                return                                    # 관제가 없으면 버림 (접속하면 status부터 다시 보냄)
            try:
                client.sendall(b"".join(m.encode(x) for x in msgs))
            except OSError:
                pass                                      # 끊김은 받는 쪽 스레드가 정리

    # TCP 명령 (관제가 접속, 4.1)
    def _tcp_loop(self) -> None:
        while self.running:
            try:
                conn, addr = self.tcp.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._send_lock:
                if self._client is not None:              # 관제가 다시 접속하면 새 연결만 씀
                    try:
                        self._client.close()
                    except OSError:
                        pass
                self._client = conn
            print(f"관제 접속: {addr[0]}")
            with self.lock:
                self.core.log("link", text=f"관제 접속 {addr[0]}")
                self.core.connected()
            self._flush()
            threading.Thread(target=self._read_client, args=(conn,), daemon=True).start()

    def _read_client(self, conn: socket.socket) -> None:
        reader = m.LineReader()
        try:
            while self.running:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                for line in reader.feed(chunk):
                    self._on_line(line)
                self._flush()
        except (OSError, m.MessageError):
            pass
        with self._send_lock:
            if self._client is conn:
                self._client = None
        print("관제 연결 끊김 (생존 신호가 1초 넘게 없으면 멈춤)")

    def _on_line(self, line: bytes) -> None:
        now = time.monotonic()
        try:
            msg = m.decode(line)
        except m.UnknownTopic as e:
            with self.lock:
                self.core.log("ignored", text=str(e))
            return
        except m.MessageError as e:                       # 형식이 틀린 명령 → reject (3장)
            topic = _topic_of(line)
            with self.lock:
                self.core.log("bad_msg", text=str(e))
                if m.split_car_topic(topic):
                    self.core.reject(topic, f"형식이 틀림: {e}")
            return
        with self.lock:
            self.core.handle(msg, now)

    # UDP 손 명령 (4.1)
    def _udp_loop(self) -> None:
        seq = m.SeqFilter()
        while self.running:
            try:
                data = self.udp.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                msg = m.decode(data)
            except m.MessageError:
                continue
            if msg["topic"] != m.car_topic(self.core.car_id, m.HAND) or not seq.accept(msg):
                continue
            with self.lock:
                self.core.handle(msg, time.monotonic())

    # 시리얼: 최신 손 명령 하나씩, OK를 받은 뒤 다음 것 (9.4)
    def _serial_loop(self) -> None:
        """켜질 때는 P만 보냄 (서보를 움직이지 않음). 3번 실패하면 닫고 1초마다 다시 열어 P (11장)."""
        last_talk = 0.0
        while self.running:
            if not self.serial.is_open and not self.serial.open():
                with self.lock:
                    self.core.serial_result(False, "포트를 열 수 없음")
                self._flush()
                time.sleep(SERIAL_RETRY_S)
                continue
            with self.lock:
                job = self.core.take_hand()
                ping = not self.core.serial_ok or time.monotonic() - last_talk > SERIAL_IDLE_PING_S
            if job is None and not ping:
                time.sleep(0.005)
                continue
            reply = self.serial.exchange(m.serial_move(*job) if job else m.serial_ping())
            last_talk = time.monotonic()
            with self.lock:
                if reply is not None and not reply[0]:
                    self.core.log("serial", text=f"ERR {reply[1]}")   # 관제가 범위를 잘못 보냄 (7장)
                self.core.serial_result(reply is not None)
                gave_up = self.core.serial_fails >= self.core.c["serial_max_fail"]
            self._flush()
            if gave_up:
                self.serial.close()
                time.sleep(SERIAL_RETRY_S)

    # 센서 주기 루프 (메인 스레드)
    def run(self) -> None:
        next_tick = time.monotonic()
        try:
            while self.running:
                now = time.monotonic()
                with self.lock:
                    driving = self.core.state == "driving"
                sensors = self.hw.read_sensors() if driving else None
                with self.lock:
                    left, right = self.core.tick(sensors, now)
                self.hw.drive(left, right)
                self._flush()
                next_tick += self.period
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_tick = time.monotonic()          # 밀렸으면 따라잡지 않음
        finally:
            self.hw.stop()


def _topic_of(line: bytes) -> str:
    try:
        topic = json.loads(line).get("topic")
    except (ValueError, AttributeError):
        return ""
    return topic if isinstance(topic, str) else ""


def main() -> None:
    from pi.car_service.hardware import RaspbotHW, SerialLink

    car = config.or_exit(config.load_car)          # 속도 값이 0이면 여기서 멈춤 (1차 시험 전)
    mp = config.or_exit(config.load_map)
    net = config.or_exit(config.load_network)
    sender = m.Sender()
    log = PiLog(car["log_dir"], sender.now)
    core = CarCore(car, mp, net["timing"], sender, log)
    if not core.line_check_on:
        print("[경고] map.yaml에 segment_s가 0인 정거장이 있어 정지선 놓침 확인(line_missed)을 끔. 1차 시험 후 채우세요.")
    me = net["cars"][str(car["car_id"])]
    hw = RaspbotHW()
    link = SerialLink(car["serial_port"], car["serial_baud"], car["serial_timeout_s"])
    service = Service(core, hw, link, me["cmd_port"], me["hand_port"], car["sensor_hz"])
    print(f"작업차 서비스: 작업차 {car['car_id']}, 명령 TCP {me['cmd_port']}, 손 UDP {me['hand_port']}, "
          f"로그 {log.path}")
    log("start", car=car["car_id"])
    service.start()
    try:
        service.run()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()


if __name__ == "__main__":
    main()
