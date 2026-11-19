"""
메시지 이름·필드·검사, 보내기·받기 도우미 (노트북·파이 공통)

기준 문서: docs/05_인터페이스_정의.md 3~8장.
이 파일과 문서가 다르면 문서를 기준으로 고친다. tests/test_messages.py가 문서와 이 파일을 비교한다.

쓰는 법 (예):
    from shared import messages as m

    out = m.Sender()                              # 보내는 쪽마다 하나 (topic별 seq를 셈)
    msg = out.make(m.car_topic(1, m.GOTO), station="storage_2")
    sock.sendall(m.encode(msg))

    msg = m.decode(line)                          # 형식이 틀리면 MessageError, 모르는 topic이면 UnknownTopic
    car, name = m.split_car_topic(msg["topic"])   # ("car1/goto") -> (1, "goto")

정거장 ID가 map.yaml에 있는지는 여기서 보지 않는다 (shared/config.py에서 확인).
파이의 Python 버전에 맞추기 위해 3.9 문법까지만 쓴다.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 이름 (topic)
# ---------------------------------------------------------------------------

CAR_IDS = (1, 2)

# 관제 → 파이 (4.1)
HAND = "hand"                  # UDP
GOTO = "goto"                  # 이하 TCP
SET_STATION = "set_station"
STOP = "stop"
RESUME = "resume"
HEARTBEAT = "heartbeat"
TIME_SYNC = "time_sync"
TO_PI = (HAND, GOTO, SET_STATION, STOP, RESUME, HEARTBEAT, TIME_SYNC)

# 파이 → 관제 (4.2)
STATUS = "status"
ARRIVED = "arrived"
ERROR = "error"
REJECT = "reject"
FROM_PI = (STATUS, ARRIVED, ERROR, REJECT)

CAR_MESSAGES = TO_PI + FROM_PI

# 노트북 안 (5장)
HAND_POSE = "hand/pose"        # 손 인식 → 관제
HAND_CALIB = "hand/calib"      # 관제 → 손 인식
VISION_STATE = "vision/state"  # 맵 비전 → 관제

# 조종 화면 (6장)
UI_STATE = "ui/state"          # 관제 → 브라우저
UI_ALERT = "ui/alert"          # 관제 → 브라우저
UI_CMD = "ui/cmd"              # 브라우저 → 관제

FIXED_TOPICS = (HAND_POSE, HAND_CALIB, VISION_STATE, UI_STATE, UI_ALERT, UI_CMD)


def car_topic(car: int, name: str) -> str:
    """car_topic(1, "goto") -> "car1/goto"."""
    if car not in CAR_IDS:
        raise ValueError(f"작업차 번호는 1 또는 2: {car!r}")
    if name not in CAR_MESSAGES:
        raise ValueError(f"작업차 메시지 이름이 아님: {name!r}")
    return f"car{car}/{name}"


def split_car_topic(topic: str) -> Optional[Tuple[int, str]]:
    """"car1/goto" -> (1, "goto"). 작업차 메시지가 아니면 None."""
    if not topic.startswith("car") or "/" not in topic:
        return None
    head, name = topic.split("/", 1)
    num = head[3:]
    if not num.isdigit() or int(num) not in CAR_IDS or name not in CAR_MESSAGES:
        return None
    return int(num), name


# ---------------------------------------------------------------------------
# 정해진 값 (문자열 목록)
# ---------------------------------------------------------------------------

PI_STATES = ("idle", "driving", "arrived", "stopped", "error")   # 4.4
MODES = ("idle", "manual", "teach", "auto", "stopped")           # 6.1 cars.mode
UI_SET_MODES = ("idle", "manual", "teach")                       # 6.2 set_mode
MARKER_STATES = ("occupied", "empty", "unknown")                 # 5.3
BENCH_STATES = ("empty", "filling", "filled")                    # 6.1
CALIB_STEPS = ("open", "fist", "reset")                          # 5.2
ALERT_LEVELS = ("info", "warn", "error")                         # 6.1
RESULTS = ("success", "fail")                                    # 6.2 mark_result
SLOT_KEYS = ("1", "2", "3")

# 8장 오류·알림 코드
PI_ERROR_CODES = ("line_lost", "serial_fail", "line_missed", "link_lost")
ALERT_CODES = (
    "car_link_lost", "goto_timeout", "cock_open", "bench_not_empty",
    "vial_not_found", "storage_empty", "task_done", "hand_lost",
    "hand_service_down", "vision_service_down", "cmd_rejected",
)
ALL_CODES = PI_ERROR_CODES + ALERT_CODES

# 4.4 상태별로 받는 명령: accept = 받음(O), reject = reject로 거절(X), drop = 조용히 버림(—)
ACCEPT, REJECT_CMD, DROP = "accept", "reject", "drop"
PI_CMD_POLICY: Dict[str, Dict[str, str]] = {
    #               idle        driving     arrived  stopped  error
    HAND:        dict(zip(PI_STATES, (DROP, DROP, ACCEPT, DROP, DROP))),
    GOTO:        dict(zip(PI_STATES, (REJECT_CMD, REJECT_CMD, ACCEPT, ACCEPT, REJECT_CMD))),
    SET_STATION: dict(zip(PI_STATES, (ACCEPT, REJECT_CMD, ACCEPT, ACCEPT, ACCEPT))),
    STOP:        dict(zip(PI_STATES, (ACCEPT,) * 5)),
    RESUME:      dict(zip(PI_STATES, (REJECT_CMD, REJECT_CMD, REJECT_CMD, ACCEPT, REJECT_CMD))),
    HEARTBEAT:   dict(zip(PI_STATES, (ACCEPT,) * 5)),
    TIME_SYNC:   dict(zip(PI_STATES, (ACCEPT,) * 5)),
}


def pi_policy(cmd: str, state: str) -> str:
    """파이가 지금 상태에서 그 명령을 어떻게 다루는지: "accept" / "reject" / "drop"."""
    return PI_CMD_POLICY[cmd][state]


# ---------------------------------------------------------------------------
# 필드 검사 도구
# ---------------------------------------------------------------------------

Check = Callable[[Any], Optional[str]]   # 맞으면 None, 틀리면 이유 문자열


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def c_bool(v: Any) -> Optional[str]:
    return None if isinstance(v, bool) else "불(true/false)이어야 함"


def c_str(v: Any) -> Optional[str]:
    return None if isinstance(v, str) and v != "" else "빈 문자열이 아닌 문자열이어야 함"


def c_num(v: Any) -> Optional[str]:
    return None if _is_num(v) else "숫자여야 함"


def c_int(lo: Optional[int] = None, hi: Optional[int] = None) -> Check:
    def check(v: Any) -> Optional[str]:
        if not _is_int(v):
            return "정수여야 함"
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return f"{lo}~{hi} 범위여야 함"
        return None
    return check


def c_range(lo: float, hi: float) -> Check:
    def check(v: Any) -> Optional[str]:
        if not _is_num(v):
            return "숫자여야 함"
        if v < lo or v > hi:
            return f"{lo}~{hi} 범위여야 함"
        return None
    return check


def c_enum(values: Tuple[str, ...]) -> Check:
    def check(v: Any) -> Optional[str]:
        return None if v in values else f"{list(values)} 중 하나여야 함"
    return check


def c_car(v: Any) -> Optional[str]:
    return None if _is_int(v) and v in CAR_IDS else "작업차 번호 1 또는 2여야 함"


def c_nullable(inner: Check) -> Check:
    def check(v: Any) -> Optional[str]:
        return None if v is None else inner(v)
    return check


def c_list(inner: Check, length: Optional[int] = None) -> Check:
    def check(v: Any) -> Optional[str]:
        if not isinstance(v, list):
            return "배열이어야 함"
        if length is not None and len(v) != length:
            return f"길이가 {length}여야 함"
        for i, item in enumerate(v):
            why = inner(item)
            if why:
                return f"[{i}] {why}"
        return None
    return check


def c_obj(fields: Dict[str, Check]) -> Check:
    """정해진 키가 모두 있는 객체. 모르는 키는 무시."""
    def check(v: Any) -> Optional[str]:
        if not isinstance(v, dict):
            return "객체여야 함"
        for key, inner in fields.items():
            if key not in v:
                return f"'{key}' 없음"
            why = inner(v[key])
            if why:
                return f".{key}: {why}"
        return None
    return check


def c_map(keys: Tuple[str, ...], inner: Check) -> Check:
    """키가 정확히 keys인 객체 (예: 보관함 칸 "1","2","3")."""
    def check(v: Any) -> Optional[str]:
        if not isinstance(v, dict):
            return "객체여야 함"
        if set(v) != set(keys):
            return f"키가 {list(keys)}여야 함"
        for key in keys:
            why = inner(v[key])
            if why:
                return f".{key}: {why}"
        return None
    return check


# ---------------------------------------------------------------------------
# 메시지별 필드 (필수 / 있어도 되는 것)
# ---------------------------------------------------------------------------

ANGLE = c_int(0, 180)
STATION = c_str   # 정거장 ID 목록은 map.yaml에 있으므로 config.py에서 확인
MARKER = c_enum(MARKER_STATES)

# name -> (필수 필드, 있어도 되는 필드)
CAR_FIELDS: Dict[str, Tuple[Dict[str, Check], Dict[str, Check]]] = {
    HAND: ({"angles": c_list(ANGLE, 6), "ms": c_int(0, 1000)}, {}),
    GOTO: ({"station": STATION}, {}),
    SET_STATION: ({"station": STATION}, {}),
    STOP: ({}, {"reason": c_str}),
    RESUME: ({}, {}),
    HEARTBEAT: ({}, {}),
    TIME_SYNC: ({"now": c_num}, {}),
    STATUS: ({"state": c_enum(PI_STATES), "station": c_nullable(STATION),
              "moving_to": c_nullable(STATION), "serial_ok": c_bool}, {}),
    ARRIVED: ({"station": STATION}, {}),
    ERROR: ({"code": c_enum(PI_ERROR_CODES), "text": c_str}, {}),
    REJECT: ({"cmd": c_str, "state": c_enum(PI_STATES), "text": c_str}, {}),
}

CAR_STATE_FIELDS: Dict[str, Check] = {          # ui/state 의 cars."1" 안
    "mode": c_enum(MODES),
    "link": c_bool,
    "pi_state": c_nullable(c_enum(PI_STATES)),
    "station": c_nullable(STATION),
    "moving_to": c_nullable(STATION),
    "task": c_nullable(c_str),
    "step": c_nullable(c_str),
    "repeat": c_bool,
    "cycle": c_int(0),
    "recording_steps": c_nullable(c_int(0)),
    "result_pending": c_bool,
    "result_cycle": c_nullable(c_int(1)),
    "video_url": c_str,
    "allowed": c_list(c_str),
}

TASK_ENTRY_FIELDS: Dict[str, Check] = {"id": c_str, "name": c_str, "car": c_car, "created": c_str}

FIXED_FIELDS: Dict[str, Tuple[Dict[str, Check], Dict[str, Check]]] = {
    HAND_POSE: ({"detected": c_bool,
                 "fingers": c_nullable(c_list(c_range(0.0, 1.0), 5)),
                 "wrist": c_nullable(c_range(-90.0, 90.0)),
                 "calibrated": c_bool,
                 "fps": c_num}, {}),
    HAND_CALIB: ({"step": c_enum(CALIB_STEPS)}, {}),
    VISION_STATE: ({"slots": c_map(SLOT_KEYS, MARKER),
                    "bench": MARKER,
                    "cock_led": c_nullable(c_bool),
                    "fps": c_num}, {}),
    UI_STATE: ({"selected_car": c_car,
                "cars": c_map(("1", "2"), c_obj(CAR_STATE_FIELDS)),
                "bench": c_enum(BENCH_STATES),
                "bench_vial": MARKER,
                "slots": c_map(SLOT_KEYS, MARKER),
                "cock_led": c_nullable(c_bool),
                "hand": c_obj({"service": c_bool, "detected": c_bool, "calibrated": c_bool}),
                "vision": c_obj({"service": c_bool}),
                "map_video_url": c_str,
                "tasks": c_list(c_obj(TASK_ENTRY_FIELDS))}, {}),
    UI_ALERT: ({"level": c_enum(ALERT_LEVELS), "car": c_nullable(c_car),
                "code": c_enum(ALL_CODES), "text": c_str}, {}),
    # ui/cmd 는 action마다 필드가 달라 UI_ACTIONS로 따로 검사
    UI_CMD: ({"action": c_str}, {}),
}

# 6.2 ui/cmd 의 action -> 필수 필드
UI_ACTIONS: Dict[str, Dict[str, Check]] = {
    "select_car": {"car": c_car},
    "set_mode": {"car": c_car, "mode": c_enum(UI_SET_MODES)},
    "record_end": {"car": c_car, "name": c_str},
    "goto": {"car": c_car, "station": STATION},
    "set_station": {"car": c_car, "station": STATION},
    "start": {"car": c_car, "task": c_str, "repeat": c_bool},
    "stop": {"car": c_car},
    "stop_all": {},
    "mark_result": {"car": c_car, "cycle": c_int(1), "result": c_enum(RESULTS)},
    "delete_task": {"task": c_str},
    "hand_calib": {"step": c_enum(CALIB_STEPS)},
}


# ---------------------------------------------------------------------------
# 검사
# ---------------------------------------------------------------------------

class MessageError(ValueError):
    """형식이 틀린 메시지. 받는 쪽은 버리고 로그에 남긴다 (TCP 명령이면 reject로 알림)."""


class UnknownTopic(MessageError):
    """모르는 topic. 받는 쪽은 무시하고 로그에 한 줄 남긴다 (3장)."""


def _fields_for(topic: str) -> Tuple[Dict[str, Check], Dict[str, Check]]:
    if topic in FIXED_FIELDS:
        return FIXED_FIELDS[topic]
    parsed = split_car_topic(topic)
    if parsed is None:
        raise UnknownTopic(f"모르는 topic: {topic!r}")
    return CAR_FIELDS[parsed[1]]


def _check_fields(msg: Dict[str, Any], required: Dict[str, Check],
                  optional: Dict[str, Check], where: str) -> None:
    for key, check in required.items():
        if key not in msg:
            raise MessageError(f"{where}: 필드 '{key}' 없음")
        why = check(msg[key])
        if why:
            raise MessageError(f"{where}: '{key}' {why}")
    for key, check in optional.items():
        if key in msg:
            why = check(msg[key])
            if why:
                raise MessageError(f"{where}: '{key}' {why}")


def validate(msg: Any) -> Dict[str, Any]:
    """메시지가 인터페이스 정의에 맞는지 검사. 맞으면 msg를 그대로 돌려준다. 모르는 필드는 무시."""
    if not isinstance(msg, dict):
        raise MessageError("메시지는 JSON 객체여야 함")
    topic = msg.get("topic")
    if not isinstance(topic, str):
        raise MessageError("'topic' 없음")
    required, optional = _fields_for(topic)
    _check_fields(msg, {"seq": c_int(0), "t": c_num}, {}, topic)
    _check_fields(msg, required, optional, topic)
    if topic == UI_CMD:
        action = msg["action"]
        if action not in UI_ACTIONS:
            raise MessageError(f"ui/cmd: 모르는 action {action!r}")
        _check_fields(msg, UI_ACTIONS[action], {}, f"ui/cmd {action}")
    return msg


# ---------------------------------------------------------------------------
# 보내기·받기
# ---------------------------------------------------------------------------

class Sender:
    """보내는 쪽 하나. topic마다 seq를 0부터 1씩 올리고, 보낸 시각 t를 붙인다.

    time_offset: 파이는 관제의 time_sync로 받은 (노트북 시각 − 파이 시각)을 넣는다.
                 노트북 서비스는 0 그대로 둔다.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._seq: Dict[str, int] = {}
        self._clock = clock
        self.time_offset = 0.0

    def now(self) -> float:
        """메시지와 로그에 쓰는 시각."""
        return self._clock() + self.time_offset

    def make(self, topic: str, **fields: Any) -> Dict[str, Any]:
        seq = self._seq.get(topic, 0)
        msg: Dict[str, Any] = {"topic": topic, "seq": seq, "t": round(self.now(), 3)}
        msg.update(fields)
        validate(msg)               # 틀린 메시지는 보내기 전에 막는다
        self._seq[topic] = seq + 1
        return msg

    def apply_time_sync(self, laptop_now: float) -> None:
        """파이: time_sync를 받으면 호출. 시스템 시계는 바꾸지 않는다."""
        self.time_offset = laptop_now - self._clock()


def encode(msg: Dict[str, Any]) -> bytes:
    """메시지 → JSON 한 줄 bytes (끝에 \\n). TCP·WebSocket·UDP 모두 같은 형식."""
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(data: Any) -> Dict[str, Any]:
    """JSON 한 줄(bytes 또는 str) → 검사를 통과한 메시지.
    형식이 틀리면 MessageError, 모르는 topic이면 UnknownTopic."""
    if isinstance(data, (bytes, bytearray)):
        try:
            data = bytes(data).decode("utf-8")
        except UnicodeDecodeError as e:
            raise MessageError(f"UTF-8이 아님: {e}") from None
    try:
        msg = json.loads(data)
    except (json.JSONDecodeError, TypeError) as e:
        raise MessageError(f"JSON이 아님: {e}") from None
    return validate(msg)


class LineReader:
    """TCP로 조각나 들어오는 bytes를 줄(메시지) 단위로 나눈다.

        reader = LineReader()
        for line in reader.feed(sock.recv(4096)):
            msg = decode(line)
    """

    MAX_LINE = 1_000_000   # 줄바꿈 없이 이보다 길면 잘못된 상대로 보고 버림

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                yield line
        if len(self._buf) > self.MAX_LINE:
            self._buf = b""
            raise MessageError("줄바꿈 없는 데이터가 너무 김")


class SeqFilter:
    """UDP 순번 규칙 (3장): topic마다 마지막으로 받은 seq 이하는 버린다.
    단, 1000 넘게 작으면 보내는 쪽이 다시 켜진 것으로 보고 받아들인다."""

    RESTART_GAP = 1000

    def __init__(self) -> None:
        self._last: Dict[str, int] = {}

    def accept(self, msg: Dict[str, Any]) -> bool:
        topic, seq = msg["topic"], msg["seq"]
        last = self._last.get(topic)
        if last is not None and seq <= last and last - seq <= self.RESTART_GAP:
            return False
        self._last[topic] = seq
        return True


# ---------------------------------------------------------------------------
# 파이 ↔ micro:bit 시리얼 (7장)
# ---------------------------------------------------------------------------

SERIAL_BAUD = 115200
SERIAL_TIMEOUT_S = 0.2
SERIAL_MAX_FAIL = 3


def serial_ping() -> bytes:
    """연결 확인 명령 P."""
    return b"P\n"


def serial_move(angles: List[int], ms: int) -> bytes:
    """서보 6개 이동 명령: M a1 a2 a3 a4 a5 a6 ms."""
    why = c_list(ANGLE, 6)(angles) or c_int(0, 1000)(ms)
    if why:
        raise MessageError(f"M 명령: {why}")
    return ("M " + " ".join(str(a) for a in angles) + f" {ms}\n").encode("ascii")


def serial_servo(servo: int, angle: int, ms: int) -> bytes:
    """서보 하나 이동 명령 S (보정 도구에서만 씀)."""
    why = c_int(1, 6)(servo) or ANGLE(angle) or c_int(0, 1000)(ms)
    if why:
        raise MessageError(f"S 명령: {why}")
    return f"S {servo} {angle} {ms}\n".encode("ascii")


def parse_serial_reply(line: Any) -> Tuple[bool, str]:
    """micro:bit 응답 한 줄 → (OK인지, 나머지 글자). 예: b"ERR range" -> (False, "range")."""
    if isinstance(line, (bytes, bytearray)):
        line = bytes(line).decode("ascii", errors="replace")
    text = line.strip()
    head, _, rest = text.partition(" ")
    if head == "OK":
        return True, rest
    if head == "ERR":
        return False, rest
    raise MessageError(f"micro:bit 응답 형식이 아님: {text!r}")
