"""
AiHand 보정 도구 (노트북) — 담당: 이보연

micro:bit를 노트북 USB에 직접 꽂고 S 명령으로 서보를 하나씩 움직여 보며 편·쥔 각도,
손목 center, 주행 자세 손목 각도(drive_wrist)를 정해 data/calibration/<이름>.json 으로 저장.
참고: 시스템 설계 2.4·9.3·9.5, 인터페이스 정의 7장·9.5·9.9

실행 (저장소 맨 위에서):
  python -m laptop.tools.aihand_calib --name aihand_A --new --no-serial          기술조사 값으로 새로 만들기
  python -m laptop.tools.aihand_calib --name aihand_B --copy-from aihand_A --port COM4   A를 복사해 B 만들기
  python -m laptop.tools.aihand_calib --name aihand_A --port COM4                 있는 파일 열어 고치기

안전 규칙
- 켜질 때는 P(연결 확인)만 보낸다. 서보는 움직이지 않는다.
- 한 번에 서보 1개만, 천천히(기본 500ms) 보낸다.
- 0~180 밖 각도, 1~6 밖 서보 번호는 보내기 전에 거절한다 (micro:bit의 S는 범위 밖을 잘라서 움직이므로).
- 저장은 shared/config.py의 load_aihand() 검사를 통과할 때만 한다.

시리얼: 서비스끼리 import하지 않는 규칙 때문에 pi/car_service의 SerialLink를 쓰지 않고 여기에 짧게 따로 둔다.
응답 해석은 shared/messages.py의 parse_serial_reply()를 쓴다.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import pathlib
import sys
import tempfile
import time
from typing import Any, Dict, Optional, Protocol, Tuple

from shared import config
from shared import messages as m

BAUD = 115200            # 인터페이스 정의 7장
REPLY_TIMEOUT_S = 0.5    # 노트북에서 직접 쓰는 도구라 파이(0.2초)보다 여유 있게
MOVE_MS = 500            # 보정할 때 서보 이동 시간
WRIST = 6

# 기술조사 측정값 (시스템 설계 2.4). center 90, drive_wrist 90은 작업차에 단 뒤 다시 정함.
DEFAULT_SERVOS = [
    {"id": 1, "part": "thumb", "open": 0, "closed": 155},
    {"id": 2, "part": "index", "open": 150, "closed": 32},
    {"id": 3, "part": "middle", "open": 150, "closed": 31},
    {"id": 4, "part": "ring", "open": 150, "closed": 30},
    {"id": 5, "part": "pinky", "open": 150, "closed": 10},
    {"id": 6, "part": "wrist", "min": 0, "max": 180, "center": 90},
]
DEFAULT_MARGIN_DEG = 5
DEFAULT_DRIVE_WRIST = 90

HELP = """명령:
  try <1~5> open|closed          저장된 각도를 보내 봄 (서보 1개)
  set <1~5> open|closed <각도>   값을 바꾸고 바로 보내 봄
  center <각도>                  손목 center를 바꾸고 보냄 (서보 6)
  drive <각도>                   drive_wrist를 바꾸고 보냄 (서보 6)
  show                           지금 값 보기
  save                           검사 후 저장
  quit                           끝내기
  help                           이 설명"""


class Link(Protocol):
    def exchange(self, cmd: bytes) -> Optional[Tuple[bool, str]]: ...


# ---------------------------------------------------------------------------
# 시리얼 (micro:bit 직접 연결)
# ---------------------------------------------------------------------------

class SerialPort:
    """명령 한 줄 → OK/ERR 응답 한 줄. READY, A...$ 같은 다른 줄은 건너뜀. 응답이 없으면 None."""

    def __init__(self, port: str, timeout_s: float = REPLY_TIMEOUT_S) -> None:
        import serial  # pyserial
        self.timeout_s = timeout_s
        self._ser = serial.Serial(port, BAUD, timeout=0.05)

    def exchange(self, cmd: bytes) -> Optional[Tuple[bool, str]]:
        self._ser.reset_input_buffer()     # 쌓여 있던 상태 정보를 버림 (기술조사 코드와 같은 방식)
        self._ser.write(cmd)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            line = self._ser.readline()
            if not line:
                continue
            try:
                return m.parse_serial_reply(line)
            except m.MessageError:
                continue
        return None

    def close(self) -> None:
        self._ser.close()


# ---------------------------------------------------------------------------
# 보정 데이터 만들기·저장
# ---------------------------------------------------------------------------

def now_text() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def default_data(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "measured": now_text(),
        "margin_deg": DEFAULT_MARGIN_DEG,
        "servos": copy.deepcopy(DEFAULT_SERVOS),
        "drive_wrist": DEFAULT_DRIVE_WRIST,
    }


def copied_data(src: Dict[str, Any], name: str) -> Dict[str, Any]:
    d = copy.deepcopy(src)
    d["name"] = name
    return d


def save(data: Dict[str, Any], calib_dir: pathlib.Path = config.CALIB_DIR) -> pathlib.Path:
    """임시 폴더에 써서 load_aihand()로 검사한 뒤, 통과하면 진짜 파일과 바꾼다. 틀리면 ConfigError."""
    name = data["name"]
    data = dict(data, measured=now_text())
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / f"{name}.json").write_text(text, encoding="utf-8")
        config.load_aihand(name, pathlib.Path(d))
    calib_dir.mkdir(parents=True, exist_ok=True)
    path = config.calibration_path(name, calib_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# 명령 처리
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, data: Dict[str, Any], link: Optional[Link],
                 calib_dir: pathlib.Path = config.CALIB_DIR, move_ms: int = MOVE_MS) -> None:
        self.data = data
        self.link = link
        self.calib_dir = calib_dir
        self.move_ms = move_ms
        self.dirty = False

    # --- 보내기 ---
    def send(self, servo: int, angle: int) -> str:
        if not 1 <= servo <= 6:
            return f"서보 번호는 1~6 (지금: {servo})"
        if not 0 <= angle <= 180:
            return f"각도는 0~180 (지금: {angle}). 보내지 않음"
        if self.link is None:
            return f"(micro:bit 연결 없음) 서보 {servo} → {angle}° 는 보내지 않음"
        reply = self.link.exchange(f"S {servo} {angle} {self.move_ms}\n".encode("ascii"))
        if reply is None:
            return f"서보 {servo} → {angle}°: 응답 없음 (USB·확장보드 전원 확인)"
        ok, rest = reply
        return f"서보 {servo} → {angle}°: {'OK' if ok else 'ERR'} {rest}".rstrip()

    # --- 명령 한 줄 ---
    def handle(self, line: str) -> str:
        parts = line.split()
        if not parts:
            return ""
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "help":
                return HELP
            if cmd == "show":
                return self.show()
            if cmd == "save":
                return self.do_save()
            if cmd == "try" and len(args) == 2:
                servo, which = self._finger(args[0], args[1])
                return self.send(servo, self.data["servos"][servo - 1][which])
            if cmd == "set" and len(args) == 3:
                servo, which = self._finger(args[0], args[1])
                angle = self._angle(args[2])
                self.data["servos"][servo - 1][which] = angle
                self.dirty = True
                return self.send(servo, angle)
            if cmd in ("center", "drive") and len(args) == 1:
                angle = self._angle(args[0])
                if cmd == "center":
                    self.data["servos"][WRIST - 1]["center"] = angle
                else:
                    self.data["drive_wrist"] = angle
                self.dirty = True
                return self.send(WRIST, angle)
        except ValueError as e:
            return str(e)
        return "알 수 없는 명령. help를 입력하세요"

    def show(self) -> str:
        lines = [f"{self.data['name']}  (margin_deg {self.data['margin_deg']})"
                 + ("  * 저장 안 한 변경 있음" if self.dirty else "")]
        for s in self.data["servos"]:
            if s["part"] == "wrist":
                lines.append(f"  {s['id']} wrist   min {s['min']:>3}  max {s['max']:>3}  center {s['center']:>3}")
            else:
                lines.append(f"  {s['id']} {s['part']:<7} open {s['open']:>3}  closed {s['closed']:>3}")
        lines.append(f"  drive_wrist {self.data['drive_wrist']}")
        return "\n".join(lines)

    def do_save(self) -> str:
        try:
            path = save(self.data, self.calib_dir)
        except config.ConfigError as e:
            return f"저장 안 함: {e}"
        self.dirty = False
        return f"저장함: {path}"

    @staticmethod
    def _angle(text: str) -> int:
        try:
            v = int(text)
        except ValueError:
            raise ValueError(f"각도는 정수 (지금: {text})") from None
        if not 0 <= v <= 180:
            raise ValueError(f"각도는 0~180 (지금: {v}). 바꾸지 않음")
        return v

    @staticmethod
    def _finger(servo_text: str, which: str) -> Tuple[int, str]:
        try:
            servo = int(servo_text)
        except ValueError:
            raise ValueError(f"서보 번호는 1~5 (지금: {servo_text})") from None
        if not 1 <= servo <= 5:
            raise ValueError(f"손가락 서보는 1~5 (지금: {servo}). 손목은 center/drive 명령")
        if which not in ("open", "closed"):
            raise ValueError(f"open 또는 closed (지금: {which})")
        return servo, which


# ---------------------------------------------------------------------------
# 시작
# ---------------------------------------------------------------------------

def prepare(name: str, new: bool, copy_from: Optional[str],
            calib_dir: pathlib.Path = config.CALIB_DIR) -> Dict[str, Any]:
    """시작할 데이터를 고른다. 틀리면 ValueError (문장 그대로 출력)."""
    exists = config.calibration_path(name, calib_dir).exists()
    if new or copy_from:
        if exists:
            raise ValueError(f"{name}.json이 이미 있음. 덮어쓰지 않음 (고치려면 --new/--copy-from 없이 실행)")
        if new:
            return default_data(name)
        src = config.load_aihand(copy_from, calib_dir)
        if src is None:
            raise ValueError(f"{copy_from}.json이 없음")
        return copied_data(src, name)
    data = config.load_aihand(name, calib_dir)
    if data is None:
        raise ValueError(f"{name}.json이 없음. --new 또는 --copy-from으로 만드세요")
    return data


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="AiHand 보정 도구")
    ap.add_argument("--name", required=True, help="보정 파일 이름 (예: aihand_A)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--new", action="store_true", help="기술조사 값으로 새로 만들기")
    g.add_argument("--copy-from", metavar="NAME", help="다른 보정 파일을 복사해 만들기")
    p = ap.add_mutually_exclusive_group(required=True)
    p.add_argument("--port", help="micro:bit 시리얼 포트 (예: COM4)")
    p.add_argument("--no-serial", action="store_true", help="micro:bit 없이 값만 고치기·저장")
    ap.add_argument("--ms", type=int, default=MOVE_MS, help=f"서보 이동 시간 ms (기본 {MOVE_MS})")
    a = ap.parse_args(argv)

    try:
        data = prepare(a.name, a.new, a.copy_from)
    except (ValueError, config.ConfigError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1

    port = None
    if a.port:
        try:
            port = SerialPort(a.port)
        except Exception as e:  # noqa: BLE001 (포트 없음, 다른 프로그램이 사용 중 등)
            print(f"[오류] {a.port} 열기 실패: {e}", file=sys.stderr)
            return 1
        time.sleep(0.3)
        reply = port.exchange(b"P\n")
        if not reply or not reply[0]:
            print(f"[오류] micro:bit 응답 없음 ({a.port}). USB 연결과 micro:bit 프로그램을 확인하세요",
                  file=sys.stderr)
            port.close()
            return 1
        print(f"micro:bit 연결 확인 ({a.port}). 서보가 안 움직이면 확장보드 전원 스위치를 켜세요")

    s = Session(data, port, move_ms=a.ms)
    if a.new or a.copy_from:
        s.dirty = True
    print(s.show())
    print("help: 명령 보기")
    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                line = "quit"
            if line.lower() == "quit":
                if s.dirty and input("저장 안 한 변경이 있음. 그래도 끝낼까요? (y/N) ").strip().lower() != "y":
                    continue
                break
            out = s.handle(line)
            if out:
                print(out)
    finally:
        if port is not None:
            port.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
