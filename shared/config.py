"""
설정·보정 파일 읽기 + 검사 (노트북·파이 공통)

기준 문서: docs/05_인터페이스_정의.md 9장 (특히 9.9 설정 읽기 규칙).
- 모든 서비스는 이 모듈로만 YAML·JSON을 읽는다. 서비스가 직접 파일을 열지 않는다.
- 필수 필드가 빠졌거나, 타입이 틀렸거나, 정거장 ID가 map.yaml에 없으면 ConfigError.
  서비스는 시작할 때 or_exit()로 감싸서 오류 문장을 출력하고 바로 멈춘다.
- 보정 파일(data/calibration/*.json)이 없으면 None을 돌려준다. 서비스는 "보정 필요"로 그 기능만 막는다.
  파일이 있는데 형식이 틀리면 ConfigError (잘못된 보정값으로 서보를 움직이지 않기 위해).

쓰는 법 (예):
    from shared import config

    net = config.or_exit(config.load_network)
    mp = config.or_exit(config.load_map)
    mp.lines_to("wait_1", "storage_2")   # -> 2
    hand_a = config.or_exit(config.load_aihand, "aihand_A")   # 없으면 None

파이의 Python 버전에 맞추기 위해 3.9 문법까지만 쓴다.
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
from typing import Any, Callable, Dict, List, Optional, TypeVar

ROOT = pathlib.Path(__file__).resolve().parents[1]
NETWORK_YAML = ROOT / "shared" / "config" / "network.yaml"
MAP_YAML = ROOT / "shared" / "config" / "map.yaml"
CAR_YAML = ROOT / "pi" / "config" / "car.yaml"
LAPTOP_YAML = ROOT / "laptop" / "config" / "laptop.yaml"
CALIB_DIR = ROOT / "data" / "calibration"

CAR_KEYS = ("1", "2")
SLOT_KEYS = ("1", "2", "3")
START_WHEN = ("bench_empty", "bench_filled")
HAND_SIDES = ("right", "left")
FINGER_PARTS = ("thumb", "index", "middle", "ring", "pinky")


class ConfigError(ValueError):
    """설정 파일이 틀림. 문장에 파일 이름과 필드 위치가 들어 있다."""


T = TypeVar("T")


def or_exit(loader: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """서비스 시작용: 설정이 틀리면 오류 문장을 출력하고 바로 멈춘다 (9.9)."""
    try:
        return loader(*args, **kwargs)
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# 파일 열기
# ---------------------------------------------------------------------------

def _read_yaml(path: pathlib.Path) -> Any:
    try:
        import yaml   # pyyaml. 노트북·파이 requirements.txt에 있음
    except ImportError as e:  # pragma: no cover
        raise ConfigError("pyyaml이 설치되어 있지 않음 (pip install pyyaml)") from e
    if not path.exists():
        raise ConfigError(f"{_rel(path)}: 파일이 없음")
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"{_rel(path)}: YAML 형식이 틀림: {e}") from None


def _read_json(path: pathlib.Path) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{_rel(path)}: JSON 형식이 틀림: {e}") from None


def _rel(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# 검사 도구: 틀리면 "파일: 위치: 이유" 문장으로 ConfigError
# ---------------------------------------------------------------------------

class _Check:
    def __init__(self, source: str) -> None:
        self.source = source

    def fail(self, where: str, why: str) -> None:
        raise ConfigError(f"{self.source}: {where}: {why}")

    def obj(self, v: Any, where: str) -> Dict[str, Any]:
        if not isinstance(v, dict):
            self.fail(where, "객체(키: 값 모음)여야 함")
        return v

    def get(self, d: Dict[str, Any], key: str, where: str) -> Any:
        if key not in d:
            self.fail(f"{where}.{key}" if where else key, "필드가 없음")
        return d[key]

    def boolean(self, d: Dict[str, Any], key: str, where: str = "") -> bool:
        v = self.get(d, key, where)
        if not isinstance(v, bool):
            self.fail(self._at(where, key), "true/false여야 함")
        return v

    def text(self, d: Dict[str, Any], key: str, where: str = "") -> str:
        v = self.get(d, key, where)
        if not isinstance(v, str) or not v:
            self.fail(self._at(where, key), "빈 문자열이 아닌 문자열이어야 함")
        return v

    def choice(self, d: Dict[str, Any], key: str, values: tuple, where: str = "") -> str:
        v = self.get(d, key, where)
        if v not in values:
            self.fail(self._at(where, key), f"{list(values)} 중 하나여야 함 (지금: {v!r})")
        return v

    def integer(self, d: Dict[str, Any], key: str, where: str = "",
                lo: Optional[int] = None, hi: Optional[int] = None) -> int:
        v = self.get(d, key, where)
        if not isinstance(v, int) or isinstance(v, bool):
            self.fail(self._at(where, key), f"정수여야 함 (지금: {v!r})")
        self._range(v, lo, hi, self._at(where, key))
        return v

    def number(self, d: Dict[str, Any], key: str, where: str = "",
               lo: Optional[float] = None, hi: Optional[float] = None,
               positive: bool = False) -> float:
        v = self.get(d, key, where)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            self.fail(self._at(where, key), f"숫자여야 함 (지금: {v!r})")
        if positive and v <= 0:
            self.fail(self._at(where, key), f"0보다 커야 함 (지금: {v!r})")
        self._range(v, lo, hi, self._at(where, key))
        return float(v)

    def port(self, d: Dict[str, Any], key: str, where: str = "") -> int:
        return self.integer(d, key, where, 1, 65535)

    def keys_exactly(self, d: Dict[str, Any], keys: tuple, where: str) -> None:
        got = sorted(str(k) for k in d)
        if got != sorted(keys) or not all(isinstance(k, str) for k in d):
            self.fail(where, f"키가 정확히 {list(keys)}여야 함 (지금: {got}). 숫자 키는 따옴표로 감싸기: \"1\"")

    def _range(self, v: float, lo: Optional[float], hi: Optional[float], where: str) -> None:
        if lo is not None and v < lo:
            self.fail(where, f"{lo} 이상이어야 함 (지금: {v!r})")
        if hi is not None and v > hi:
            self.fail(where, f"{hi} 이하여야 함 (지금: {v!r})")

    @staticmethod
    def _at(where: str, key: str) -> str:
        return f"{where}.{key}" if where else key


# ---------------------------------------------------------------------------
# network.yaml (9.1)
# ---------------------------------------------------------------------------

def load_network(path: pathlib.Path = NETWORK_YAML) -> Dict[str, Any]:
    c = _Check(_rel(path))
    d = c.obj(_read_yaml(path), "(파일 전체)")

    laptop = c.obj(c.get(d, "laptop", ""), "laptop")
    c.text(laptop, "ip", "laptop")
    for key in ("web_port", "hand_pose_port", "vision_state_port", "hand_calib_port", "map_video_port"):
        c.port(laptop, key, "laptop")

    cars = c.obj(c.get(d, "cars", ""), "cars")
    c.keys_exactly(cars, CAR_KEYS, "cars")
    for n in CAR_KEYS:
        car = c.obj(cars[n], f"cars.{n}")
        c.text(car, "ip", f"cars.{n}")
        for key in ("hand_port", "cmd_port", "video_port"):
            c.port(car, key, f"cars.{n}")
        c.text(car, "aihand", f"cars.{n}")

    timing = c.obj(c.get(d, "timing", ""), "timing")
    for key in ("heartbeat_s", "link_timeout_s", "status_period_s", "reconnect_s", "service_timeout_s"):
        c.number(timing, key, "timing", positive=True)
    if timing["heartbeat_s"] >= timing["link_timeout_s"]:
        c.fail("timing", "heartbeat_s는 link_timeout_s보다 짧아야 함 (아니면 파이가 계속 멈춤)")
    return d


# ---------------------------------------------------------------------------
# map.yaml (9.2)
# ---------------------------------------------------------------------------

class MapConfig:
    """map.yaml 내용 + 정거장 도우미."""

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.raw = raw
        self.loops: Dict[str, int] = raw["loops"]
        self.cars: Dict[str, Dict[str, Any]] = raw["cars"]
        self.stations: Dict[str, Dict[str, Any]] = {s["id"]: s for s in raw["stations"]}
        self.line_check = raw["line_check"]
        self.markers = raw["markers"]
        self.vision = raw["vision"]
        self.drive_pose = raw["drive_pose"]
        self.auto = raw["auto"]

    def is_station(self, station_id: str) -> bool:
        return station_id in self.stations

    def station(self, station_id: str) -> Dict[str, Any]:
        if station_id not in self.stations:
            raise KeyError(f"map.yaml에 없는 정거장: {station_id!r}")
        return self.stations[station_id]

    def stations_of_car(self, car: int) -> List[str]:
        loop = self.cars[str(car)]["loop"]
        return [sid for sid, s in self.stations.items() if s["loop"] == loop]

    def wait_station(self, car: int) -> str:
        return self.cars[str(car)]["wait_station"]

    def lines_to(self, from_id: str, to_id: str) -> int:
        """지금 정거장에서 목표 정거장까지 넘어야 할 정지선 수 (시스템 설계 5.1, 10.2). 0이면 움직이지 않음."""
        a, b = self.station(from_id), self.station(to_id)
        if a["loop"] != b["loop"]:
            raise ValueError(f"다른 고리의 정거장: {from_id} → {to_id}")
        return (b["line"] - a["line"]) % self.loops[a["loop"]]

    def line_check_enabled(self) -> bool:
        """segment_s가 0인 정거장이 하나라도 있으면 정지선 놓침 확인을 끈다 (9.2, 1차 시험 전)."""
        return all(s["segment_s"] > 0 for s in self.stations.values())


def load_map(path: pathlib.Path = MAP_YAML) -> MapConfig:
    c = _Check(_rel(path))
    d = c.obj(_read_yaml(path), "(파일 전체)")

    loops = c.obj(c.get(d, "loops", ""), "loops")
    if not loops:
        c.fail("loops", "고리가 하나도 없음")
    for name in loops:
        c.integer(loops, name, "loops", lo=1)

    stations = c.get(d, "stations", "")
    if not isinstance(stations, list) or not stations:
        c.fail("stations", "정거장 목록(배열)이어야 함")
    seen_ids: Dict[str, int] = {}
    seen_lines: Dict[tuple, str] = {}
    for i, s in enumerate(stations):
        where = f"stations[{i}]"
        c.obj(s, where)
        sid = c.text(s, "id", where)
        where = f"stations[{i}] ({sid})"
        if sid in seen_ids:
            c.fail(where, f"정거장 id가 겹침 (stations[{seen_ids[sid]}]과 같음)")
        seen_ids[sid] = i
        c.text(s, "name", where)
        loop = c.choice(s, "loop", tuple(loops), where)
        line = c.integer(s, "line", where, lo=0, hi=loops[loop] - 1)
        if (loop, line) in seen_lines:
            c.fail(where, f"{loop} 고리의 정지선 {line}을 {seen_lines[(loop, line)]}도 쓰고 있음")
        seen_lines[(loop, line)] = sid
        c.number(s, "segment_s", where, lo=0)
        if "slot" in s:
            c.choice(s, "slot", (1, 2, 3), where)
    for name, count in loops.items():
        used = sum(1 for (lp, _) in seen_lines if lp == name)
        if used != count:
            c.fail(f"loops.{name}", f"정지선 {count}개인데 정거장은 {used}개")
    slots = sorted(s["slot"] for s in stations if "slot" in s)
    if slots != [1, 2, 3]:
        c.fail("stations", f"보관함 칸 정거장(slot 1, 2, 3)이 하나씩 있어야 함 (지금: {slots})")

    cars = c.obj(c.get(d, "cars", ""), "cars")
    c.keys_exactly(cars, CAR_KEYS, "cars")
    for n in CAR_KEYS:
        car = c.obj(cars[n], f"cars.{n}")
        loop = c.choice(car, "loop", tuple(loops), f"cars.{n}")
        wait = c.choice(car, "wait_station", tuple(seen_ids), f"cars.{n}")
        if stations[seen_ids[wait]]["loop"] != loop:
            c.fail(f"cars.{n}.wait_station", f"{wait}는 {loop} 고리의 정거장이 아님")
        if stations[seen_ids[wait]]["line"] != 0:
            c.fail(f"cars.{n}.wait_station", "대기 정거장은 정지선 번호가 0이어야 함 (시스템 설계 5.1)")
        c.choice(car, "start_when", START_WHEN, f"cars.{n}")
    if cars["1"]["loop"] == cars["2"]["loop"]:
        c.fail("cars", "두 작업차가 같은 고리를 쓰면 안 됨 (시스템 설계 1장)")

    lc = c.obj(c.get(d, "line_check", ""), "line_check")
    c.number(lc, "normal_factor", "line_check", lo=1.0)
    c.number(lc, "slow_factor", "line_check", lo=1.0)

    mk = c.obj(c.get(d, "markers", ""), "markers")
    c.text(mk, "dictionary", "markers")
    slot_ids = c.obj(c.get(mk, "slot_ids", "markers"), "markers.slot_ids")
    c.keys_exactly(slot_ids, SLOT_KEYS, "markers.slot_ids")
    marker_ids = [c.integer(slot_ids, k, "markers.slot_ids", lo=0, hi=49) for k in SLOT_KEYS]
    marker_ids.append(c.integer(mk, "bench_id", "markers", lo=0, hi=49))
    if len(set(marker_ids)) != len(marker_ids):
        c.fail("markers", f"마커 ID가 겹침: {marker_ids}")
    c.number(mk, "min_perimeter_rate", "markers", positive=True, hi=1.0)

    vi = c.obj(c.get(d, "vision", ""), "vision")
    for key in ("hidden_s", "hold_settle_s", "led_on_s"):
        c.number(vi, key, "vision", positive=True)

    dp = c.obj(c.get(d, "drive_pose", ""), "drive_pose")
    c.boolean(dp, "enabled", "drive_pose")
    c.number(dp, "settle_s", "drive_pose", lo=0)

    au = c.obj(c.get(d, "auto", ""), "auto")
    for key in ("goto_timeout_s", "cock_hold_s", "condition_check_s"):
        c.number(au, key, "auto", positive=True)
    return MapConfig(d)


# ---------------------------------------------------------------------------
# car.yaml (9.3) / laptop.yaml (9.4)
# ---------------------------------------------------------------------------

TUNED_CAR_KEYS = ("speed_normal", "speed_slow", "turn_small", "turn_big")


def load_car(path: pathlib.Path = CAR_YAML, require_tuned: bool = True) -> Dict[str, Any]:
    """require_tuned=True면 속도·조향 값이 0일 때 오류 (작업차 서비스 시작용, 9.3).
    1차 시험 전 확인·시험에서는 False로 읽는다."""
    c = _Check(_rel(path))
    d = c.obj(_read_yaml(path), "(파일 전체)")
    c.choice(d, "car_id", (1, 2))
    c.text(d, "serial_port")
    c.integer(d, "serial_baud", lo=1)
    c.number(d, "serial_timeout_s", positive=True)
    c.integer(d, "serial_max_fail", lo=1)
    c.number(d, "sensor_hz", positive=True)
    c.boolean(d, "invert_sensors")
    for key in TUNED_CAR_KEYS:
        v = c.number(d, key, lo=0)
        if require_tuned and v == 0:
            c.fail(key, "0이면 아직 정하지 않은 값. 1차 시험 후 채우세요")
    c.number(d, "line_lost_search_s", positive=True)
    c.integer(d, "stopline_min_black", lo=1, hi=4)
    c.integer(d, "stopline_confirm_reads", lo=1)
    video = c.obj(c.get(d, "video", ""), "video")
    c.integer(video, "device", "video", lo=0)
    c.integer(video, "width", "video", lo=1)
    c.integer(video, "height", "video", lo=1)
    c.number(video, "fps", "video", positive=True)
    c.port(video, "port", "video")
    c.text(d, "log_dir")
    d["log_dir"] = str(pathlib.Path(d["log_dir"]).expanduser())
    return d


def load_laptop(path: pathlib.Path = LAPTOP_YAML) -> Dict[str, Any]:
    c = _Check(_rel(path))
    d = c.obj(_read_yaml(path), "(파일 전체)")
    c.integer(d, "hand_camera_index", lo=0)
    c.choice(d, "hand_side", HAND_SIDES)
    c.integer(d, "hand_max_num", lo=1)
    c.boolean(d, "mirror")
    c.integer(d, "smooth_frames", lo=1)
    return d


# ---------------------------------------------------------------------------
# 보정 파일 (9.5, 9.6) — 없으면 None
# ---------------------------------------------------------------------------

def calibration_path(name: str, calib_dir: pathlib.Path = CALIB_DIR) -> pathlib.Path:
    return calib_dir / f"{name}.json"


def load_aihand(name: str, calib_dir: pathlib.Path = CALIB_DIR) -> Optional[Dict[str, Any]]:
    """data/calibration/<name>.json (예: aihand_A). 없으면 None."""
    path = calibration_path(name, calib_dir)
    if not path.exists():
        return None
    c = _Check(_rel(path))
    d = c.obj(_read_json(path), "(파일 전체)")
    c.text(d, "name")
    c.integer(d, "margin_deg", lo=0, hi=30)
    servos = c.get(d, "servos", "")
    if not isinstance(servos, list) or len(servos) != 6:
        c.fail("servos", "서보 6개의 배열이어야 함")
    for i, s in enumerate(servos):
        where = f"servos[{i}]"
        c.obj(s, where)
        if c.integer(s, "id", where) != i + 1:
            c.fail(f"{where}.id", f"{i + 1}이어야 함 (서보 1~6 순서대로)")
        if i < 5:
            c.choice(s, "part", (FINGER_PARTS[i],), where)
            op = c.integer(s, "open", where, lo=0, hi=180)
            cl = c.integer(s, "closed", where, lo=0, hi=180)
            if abs(op - cl) <= 2 * d["margin_deg"]:
                c.fail(where, "open과 closed 차이가 margin_deg의 2배보다 커야 함")
        else:
            c.choice(s, "part", ("wrist",), where)
            lo = c.integer(s, "min", where, lo=0, hi=180)
            hi = c.integer(s, "max", where, lo=0, hi=180)
            center = c.integer(s, "center", where, lo=0, hi=180)
            if not lo < hi or not lo <= center <= hi:
                c.fail(where, "min < max 이고 center가 그 사이여야 함")
    wrist = servos[5]
    dw = c.integer(d, "drive_wrist", lo=0, hi=180)
    if not wrist["min"] <= dw <= wrist["max"]:
        c.fail("drive_wrist", "손목 min~max 사이여야 함")
    return d


def load_topcam(calib_dir: pathlib.Path = CALIB_DIR) -> Optional[Dict[str, Any]]:
    """data/calibration/topcam.json. 없으면 None (맵 비전은 cock_led: null)."""
    path = calibration_path("topcam", calib_dir)
    if not path.exists():
        return None
    c = _Check(_rel(path))
    d = c.obj(_read_json(path), "(파일 전체)")
    c.integer(d, "camera_index", lo=0)
    w = c.integer(d, "width", lo=1)
    h = c.integer(d, "height", lo=1)
    led = c.obj(c.get(d, "cock_led", ""), "cock_led")
    rect = c.get(led, "rect", "cock_led")
    if (not isinstance(rect, list) or len(rect) != 4
            or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in rect)):
        c.fail("cock_led.rect", "[x, y, 폭, 높이] 0 이상 정수 4개여야 함")
    x, y, rw, rh = rect
    if rw <= 0 or rh <= 0 or x + rw > w or y + rh > h:
        c.fail("cock_led.rect", f"사진({w}×{h}) 안의 크기 있는 영역이어야 함")
    c.number(led, "on_threshold", "cock_led", lo=0, hi=255)
    return d


def load_operator(calib_dir: pathlib.Path = CALIB_DIR) -> Optional[Dict[str, Any]]:
    """data/calibration/operator.json (조종자 손 맞춤). 없으면 None."""
    path = calibration_path("operator", calib_dir)
    if not path.exists():
        return None
    c = _Check(_rel(path))
    d = c.obj(_read_json(path), "(파일 전체)")
    for key in ("open", "fist"):
        v = c.get(d, key, "")
        if (not isinstance(v, list) or len(v) != 5
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
            c.fail(key, "[엄지, 검지, 중지, 약지, 새끼] 숫자 5개여야 함")
    return d


# ---------------------------------------------------------------------------
# 여러 파일을 함께 보는 검사
# ---------------------------------------------------------------------------

def check_network_against_calibration(net: Dict[str, Any], calib_dir: pathlib.Path = CALIB_DIR) -> Dict[str, bool]:
    """작업차별로 network.yaml의 aihand 보정 파일이 있는지. 관제가 "보정 필요" 표시에 씀."""
    return {n: calibration_path(net["cars"][n]["aihand"], calib_dir).exists() for n in CAR_KEYS}
