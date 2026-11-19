"""
맵 위 웹캠 보정 도구 (노트북) — 담당: 유정완

웹캠 번호 선택, 콕 LED 영역 지정, LED 켜짐/꺼짐 밝기를 재서 '켜짐 기준 밝기'를
data/calibration/topcam.json 으로 저장.
참고: 시스템 설계 5.2, 인터페이스 정의 9.6

실행 (저장소 맨 위에서):  python -m laptop.tools.topcam_calib [--camera 1]
순서: ① 웹캠 고르기 ② LED 둘레를 마우스로 네모 그리기 ③ LED 켜고 Enter ④ LED 끄고 Enter → 저장
맵 비전 서비스가 웹캠을 쓰고 있으면 먼저 끄세요 (웹캠은 한 프로그램만 열 수 있음).
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from shared import config

SIZE = (1920, 1080)      # 시스템 설계 2.1 HW-02
MEASURE_S = 1.0          # 켜짐·꺼짐 밝기를 잴 시간
MIN_GAP = 30.0           # 켜짐과 꺼짐 밝기 차이가 이보다 작으면 판단이 불안정하다고 보고 저장 안 함
SHOW_WIDTH = 1280        # 화면에 보일 크기 (1080p는 화면보다 큼)
WINDOW = "topcam_calib"


def mean_brightness(frame: np.ndarray, rect: List[int]) -> float:
    """영역 평균 밝기 (0~255). 맵 비전 서비스 LedJudge와 같은 식: 흑백 사진의 영역 평균."""
    x, y, w, h = rect
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    return float(gray[y:y + h, x:x + w].mean())


def make_topcam(camera_index: int, width: int, height: int, rect: List[int],
                on_level: float, off_level: float) -> Dict[str, Any]:
    """topcam.json 내용 (인터페이스 정의 9.6). 켜짐 기준 밝기 = 켠 때와 끈 때의 중간 (시스템 설계 5.2)."""
    if on_level - off_level < MIN_GAP:
        raise ValueError(f"켠 때({on_level:.1f})와 끈 때({off_level:.1f}) 밝기 차이가 {MIN_GAP:.0f}보다 작음. "
                         "영역을 LED에 딱 맞게 작게 잡거나, 주변 조명을 확인하세요.")
    return {
        "measured": datetime.datetime.now().isoformat(timespec="seconds"),
        "camera_index": camera_index,
        "width": width,
        "height": height,
        "cock_led": {"rect": [int(v) for v in rect], "on_threshold": round((on_level + off_level) / 2, 1)},
    }


def save_topcam(data: Dict[str, Any], calib_dir=config.CALIB_DIR) -> None:
    """임시 폴더에서 config.load_topcam 검사를 통과한 뒤에만 저장 (틀린 보정값이 남지 않게)."""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "topcam.json").write_text(text, encoding="utf-8")
        config.load_topcam(pathlib.Path(d))
    calib_dir.mkdir(parents=True, exist_ok=True)
    config.calibration_path("topcam", calib_dir).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 화면에서 하는 부분 (웹캠 필요)
# ---------------------------------------------------------------------------

def open_camera(index: int) -> cv2.VideoCapture:
    # 맵 비전 서비스와 같은 방식으로 연다 (같은 해상도가 나와야 LED 영역 좌표가 맞음)
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(index)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, SIZE[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, SIZE[1])
    return cap


def show(frame: np.ndarray, text: str) -> int:
    scale = SHOW_WIDTH / frame.shape[1]
    small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame.copy()
    cv2.putText(small, text, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 5)
    cv2.putText(small, text, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
    cv2.imshow(WINDOW, small)
    return cv2.waitKey(30) & 0xFF


def choose_camera(arg: Optional[int]) -> tuple:
    """번호를 차례로 보여 주고 맵이 보이는 웹캠을 고름. Enter = 이것, n = 다음, q = 끝."""
    candidates = [arg] if arg is not None else list(range(5))
    for index in candidates:
        cap = open_camera(index)
        ok, frame = cap.read()
        if not ok:
            cap.release()
            continue
        print(f"웹캠 {index}: {frame.shape[1]}×{frame.shape[0]}. 맵이 보이면 Enter, 아니면 n")
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            key = show(frame, f"camera {index}: Enter=use  n=next  q=quit")
            if key in (13, 10):
                return index, cap
            if key == ord("n"):
                break
            if key == ord("q"):
                sys.exit(0)
        cap.release()
    print("맵 위 웹캠을 찾지 못함. USB 연결을 확인하세요.", file=sys.stderr)
    sys.exit(1)


def select_rect(frame: np.ndarray) -> List[int]:
    scale = min(1.0, SHOW_WIDTH / frame.shape[1])
    small = cv2.resize(frame, None, fx=scale, fy=scale)
    print("LED 둘레를 마우스로 작은 네모로 그리고 Enter (다시 그리려면 그냥 다시 끌기, 취소는 c)")
    x, y, w, h = cv2.selectROI(WINDOW, small, showCrosshair=True)
    if w == 0 or h == 0:
        print("영역을 고르지 않아 끝냄.")
        sys.exit(1)
    return [round(x / scale), round(y / scale), max(1, round(w / scale)), max(1, round(h / scale))]


def measure(cap: cv2.VideoCapture, rect: List[int], prompt: str) -> float:
    print(prompt)
    while True:
        ok, frame = cap.read()
        if not ok:
            continue
        x, y, w, h = rect
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 255), 3)
        level = mean_brightness(frame, rect)
        if show(frame, f"brightness {level:.1f}  Enter=measure") in (13, 10):
            break
    levels = []
    end = time.monotonic() + MEASURE_S
    while time.monotonic() < end:
        ok, frame = cap.read()
        if ok:
            levels.append(mean_brightness(frame, rect))
    value = float(np.mean(levels))
    print(f"  → 평균 밝기 {value:.1f} ({len(levels)}장)")
    return value


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="맵 위 웹캠 보정 (topcam.json)")
    ap.add_argument("--camera", type=int, help="웹캠 번호 (없으면 0번부터 차례로 보여 줌)")
    args = ap.parse_args(argv)

    index, cap = choose_camera(args.camera)
    ok, frame = cap.read()
    if not ok:
        print("웹캠에서 사진을 읽지 못함.", file=sys.stderr)
        sys.exit(1)
    height, width = frame.shape[:2]
    if (width, height) != SIZE:
        print(f"[주의] 사진이 {width}×{height}입니다 (기준 {SIZE[0]}×{SIZE[1]}). 이 크기로 저장합니다.")

    rect = select_rect(frame)
    on_level = measure(cap, rect, "LED를 켜고(콕 열기) Enter.")
    off_level = measure(cap, rect, "LED를 끄고(콕 닫기) Enter.")
    cap.release()
    cv2.destroyAllWindows()

    try:
        data = make_topcam(index, width, height, rect, on_level, off_level)
        save_topcam(data)
    except (ValueError, config.ConfigError) as e:
        print(f"[저장 안 함] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"저장함: {config.calibration_path('topcam')}  켜짐 기준 밝기 {data['cock_led']['on_threshold']}")
    print("맵 비전 서비스를 다시 켜면 새 값을 씁니다.")


if __name__ == "__main__":
    main()
