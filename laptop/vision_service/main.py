"""
맵 비전 서비스 (노트북) — 담당: 유정완

하는 일: 맵 위 웹캠에서 ArUco 마커(DICT_4X4_50) 찾기 → 보관함 칸·작업대 마커가 가려졌는지, 콕 LED 켜짐 판단.
        맵 위 영상을 HTTP MJPEG(:8090/video)로 제공.
입력: 맵 위 웹캠, data/calibration/topcam.json, shared/config/map.yaml
출력: vision/state (UDP 127.0.0.1:5101 → 관제 서비스), 맵 위 영상
관련 SR: SR-23, SR-25, SR-28, SR-32
참고: 시스템 설계 5.2, 인터페이스 정의 5.3

실행 (저장소 맨 위에서):
    python -m laptop.vision_service.main                 # topcam.json의 camera_index
    python -m laptop.vision_service.main --camera 1      # 웹캠 번호 직접 지정
    python -m laptop.vision_service.main --video map.mp4 # 웹캠 없이 녹화 파일로 시험 (끝나면 처음부터)
"""

from __future__ import annotations

import argparse
import collections
import socket
import sys
import time
from typing import Any, Dict, Optional

import cv2
import numpy as np

from shared import config
from shared import messages as m
from shared.mjpeg import MjpegServer

SEND_PERIOD_S = 0.1          # vision/state 1초 10번 (인터페이스 정의 5.3)
DEFAULT_SIZE = (1920, 1080)  # topcam.json이 없을 때 웹캠 해상도 (시스템 설계 2.1 HW-02)
STREAM_WIDTH = 960           # 조종 화면에 보낼 영상 가로 (1080p 그대로는 무거움)
STREAM_PERIOD_S = 0.1        # 조종 화면 영상 1초 10장
STREAM_JPEG_QUALITY = 70
NO_FRAME_S = 1.0             # 웹캠 사진이 이만큼 안 오면 vision/state를 멈춤 → 관제가 "맵 비전 꺼짐"으로 봄


class MarkerJudge:
    """마커 하나의 1단계 판단 (시스템 설계 5.2).
    안 보임이 hidden_s 넘게 이어지면 occupied, 보임이 이어지면 empty.
    처음 정해지기 전에는 unknown. 짧게 깜빡이는 동안은 앞서 정한 값을 그대로 둔다."""

    def __init__(self, hidden_s: float) -> None:
        self.hidden_s = hidden_s
        self.state = "unknown"
        self._visible: Optional[bool] = None
        self._since = 0.0

    def update(self, visible: bool, now: float) -> str:
        if visible != self._visible:
            self._visible, self._since = visible, now
        if now - self._since > self.hidden_s:
            self.state = "empty" if visible else "occupied"
        return self.state


class LedJudge:
    """콕 LED: 영역 평균 밝기가 on_threshold보다 높은 상태가 on_s 이상 이어지면 켜짐. 내려가면 바로 꺼짐."""

    def __init__(self, rect: list, on_threshold: float, on_s: float) -> None:
        self.x, self.y, self.w, self.h = rect
        self.on_threshold = on_threshold
        self.on_s = on_s
        self.brightness = 0.0
        self._bright_since: Optional[float] = None

    def update(self, gray: np.ndarray, now: float) -> bool:
        # topcam_calib.py와 같은 식 (흑백 사진의 영역 평균)
        self.brightness = float(gray[self.y:self.y + self.h, self.x:self.x + self.w].mean())
        if self.brightness > self.on_threshold:
            if self._bright_since is None:
                self._bright_since = now
            return now - self._bright_since >= self.on_s
        self._bright_since = None
        return False


def make_detector(markers: Dict[str, Any]) -> "cv2.aruco.ArucoDetector":
    name = markers["dictionary"]
    if not hasattr(cv2.aruco, name):
        raise config.ConfigError(f"map.yaml: markers.dictionary: OpenCV에 없는 사전 {name!r}")
    params = cv2.aruco.DetectorParameters()
    params.minMarkerPerimeterRate = markers["min_perimeter_rate"]
    return cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name)), params)


class Vision:
    """사진 한 장씩 넣으면 vision/state 필드를 만든다. 웹캠·네트워크와 떨어져 있어 사진 파일로 시험할 수 있다."""

    def __init__(self, mp: config.MapConfig, topcam: Optional[Dict[str, Any]]) -> None:
        self.detector = make_detector(mp.markers)
        hidden_s = mp.vision["hidden_s"]
        self.slot_ids = {k: v for k, v in mp.markers["slot_ids"].items()}
        self.bench_id = mp.markers["bench_id"]
        self.slots = {k: MarkerJudge(hidden_s) for k in self.slot_ids}
        self.bench = MarkerJudge(hidden_s)
        self.led: Optional[LedJudge] = None
        if topcam is not None:
            led = topcam["cock_led"]
            self.led = LedJudge(led["rect"], led["on_threshold"], mp.vision["led_on_s"])
        self.cock_led: Optional[bool] = None
        self.seen: set = set()
        self.corners: tuple = ()
        self.ids: Optional[np.ndarray] = None

    def process(self, frame: np.ndarray, now: float) -> None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        self.corners, self.ids, _ = self.detector.detectMarkers(gray)
        self.seen = set() if self.ids is None else {int(i) for i in self.ids.flatten()}
        for key, judge in self.slots.items():
            judge.update(self.slot_ids[key] in self.seen, now)
        self.bench.update(self.bench_id in self.seen, now)
        if self.led is not None:
            self.cock_led = self.led.update(gray, now)

    def fields(self) -> Dict[str, Any]:
        return {"slots": {k: j.state for k, j in self.slots.items()},
                "bench": self.bench.state,
                "cock_led": self.cock_led}

    def draw(self, frame: np.ndarray) -> np.ndarray:
        """조종 화면용: 찾은 마커 테두리, LED 영역, 판단 결과 (글자는 영어만 됨)."""
        out = frame.copy()
        if self.ids is not None:
            cv2.aruco.drawDetectedMarkers(out, self.corners, self.ids)
        if self.led is not None:
            color = (0, 255, 0) if self.cock_led else (0, 0, 255)
            cv2.rectangle(out, (self.led.x, self.led.y), (self.led.x + self.led.w, self.led.y + self.led.h), color, 3)
        f = self.fields()
        text = " ".join(f"S{k}:{v}" for k, v in f["slots"].items()) + f"  B:{f['bench']}  LED:{f['cock_led']}"
        cv2.putText(out, text, (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 8)
        cv2.putText(out, text, (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3)
        return out


class FpsCounter:
    """최근 1초 처리 속도."""

    def __init__(self) -> None:
        self._times: collections.deque = collections.deque()

    def tick(self, now: float) -> None:
        self._times.append(now)
        while self._times and now - self._times[0] > 1.0:
            self._times.popleft()

    @property
    def fps(self) -> float:
        return float(len(self._times))


# ---------------------------------------------------------------------------
# 웹캠
# ---------------------------------------------------------------------------

def open_camera(index: int, size: tuple) -> cv2.VideoCapture:
    # Windows는 DirectShow가 1080p 설정을 잘 받음. MJPG로 받아야 USB2에서 1080p 30장이 나옴
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(index)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
    return cap


def pick_camera_index(arg: Optional[int], topcam: Optional[Dict[str, Any]]) -> int:
    if arg is not None:
        return arg
    if topcam is not None:
        return topcam["camera_index"]
    # 보정 전: 손 인식 웹캠이 아닌 번호 (웹캠 2대 = 0, 1)
    hand = config.or_exit(config.load_laptop)["hand_camera_index"]
    return 1 if hand == 0 else 0


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="맵 비전 서비스")
    ap.add_argument("--camera", type=int, help="맵 위 웹캠 번호 (기본: topcam.json의 camera_index)")
    ap.add_argument("--video", help="웹캠 대신 읽을 녹화 파일 (시험용)")
    args = ap.parse_args(argv)

    net = config.or_exit(config.load_network)
    mp = config.or_exit(config.load_map)
    topcam = config.or_exit(config.load_topcam)
    if topcam is None:
        print("[보정 필요] data/calibration/topcam.json 없음 → 콕 LED 판단 끔 (cock_led: null). "
              "python -m laptop.tools.topcam_calib 로 보정하세요.")
    size = (topcam["width"], topcam["height"]) if topcam else DEFAULT_SIZE

    try:
        vision = Vision(mp, topcam)
    except config.ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        sys.exit(1)

    if args.video:
        source, cap = args.video, cv2.VideoCapture(args.video)
    else:
        index = pick_camera_index(args.camera, topcam)
        source, cap = f"웹캠 {index}", open_camera(index, size)

    stream = MjpegServer(net["laptop"]["map_video_port"])
    stream.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = ("127.0.0.1", net["laptop"]["vision_state_port"])
    out = m.Sender()
    fps = FpsCounter()
    print(f"맵 비전 서비스: {source} → vision/state UDP {dest[1]}, "
          f"영상 http://localhost:{stream.port}/video")

    checked_size = False
    last_frame = time.monotonic()
    next_send = next_stream = 0.0
    frame_wait = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 30) if args.video else 0.0
    while True:
        ok, frame = cap.read()
        now = time.monotonic()
        if not ok:
            if args.video:                         # 파일 끝 → 처음부터
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            if now - last_frame > NO_FRAME_S:
                print(f"[웹캠 오류] {source}에서 사진이 안 옴. 다시 여는 중… (그동안 vision/state 안 보냄)")
                cap.release()
                time.sleep(1.0)
                cap = open_camera(index, size)
                last_frame = time.monotonic()
            continue
        last_frame = now

        if not checked_size:
            checked_size = True
            h, w = frame.shape[:2]
            print(f"사진 크기 {w}×{h}")
            if vision.led is not None and (w, h) != size:
                print(f"[보정 확인] topcam.json은 {size[0]}×{size[1]} 기준인데 사진은 {w}×{h} → 콕 LED 판단 끔. "
                      "다시 보정하세요.")
                vision.led, vision.cock_led = None, None

        vision.process(frame, now)
        fps.tick(now)

        if now >= next_send:
            next_send = now + SEND_PERIOD_S
            msg = out.make(m.VISION_STATE, fps=fps.fps, **vision.fields())
            try:
                sock.sendto(m.encode(msg), dest)
            except OSError as e:   # 관제가 꺼져 있어도 계속 돈다
                print(f"[보내기 실패] {e}")

        if now >= next_stream:
            next_stream = now + STREAM_PERIOD_S
            shown = vision.draw(frame)
            h, w = shown.shape[:2]
            if w > STREAM_WIDTH:
                shown = cv2.resize(shown, (STREAM_WIDTH, h * STREAM_WIDTH // w))
            ok, jpg = cv2.imencode(".jpg", shown, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY])
            if ok:
                stream.publish(jpg.tobytes())

        if frame_wait:
            time.sleep(frame_wait)


if __name__ == "__main__":
    main()
