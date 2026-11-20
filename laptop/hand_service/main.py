"""
손 인식 서비스 (노트북) — 담당: 이보연

하는 일: 내장 웹캠 사진 → MediaPipe 손 관절 점 21개 → 손가락 굽힘 정도 r(0~1), 손목 회전 각도.
입력: 내장 웹캠, hand/calib (UDP 5102), laptop/config/laptop.yaml, data/calibration/operator.json
출력: hand/pose (UDP 127.0.0.1:5100 → 관제 서비스),
      손 영상 (HTTP MJPEG :8091/video → 브라우저. 거울 사진 + 관절 점, 320×240, 1초 10장)
관련 SR: SR-01, SR-09, SR-32
참고: 시스템 설계 9.2·12장, 인터페이스 정의 2장·5.1·5.2

실행 (저장소 맨 위에서):
    python -m laptop.hand_service.main                  # laptop.yaml의 hand_camera_index
    python -m laptop.hand_service.main --camera 1       # 웹캠 번호 직접 지정
    python -m laptop.hand_service.main --video hand.mp4 # 웹캠 없이 녹화 파일로 시험 (끝나면 처음부터)
    python -m laptop.hand_service.main --show           # 확인용 창: 계산한 값을 사진 위에 글자로 보여 줌
                                                        # (조종 화면의 손 영상에는 글자를 넣지 않음, 설계 12장)

MediaPipe 1.x에는 예전 mp.solutions.hands가 없어 HandLandmarker(Tasks)를 쓴다.
모델 파일: laptop/hand_service/models/hand_landmarker.task
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import math
import pathlib
import socket
import sys
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from shared import config
from shared import messages as m
from shared.mjpeg import MjpegServer

MODEL_PATH = pathlib.Path(__file__).resolve().parent / "models" / "hand_landmarker.task"
CAMERA_SIZE = (640, 480)
LOST_SEND_PERIOD_S = 0.1     # 손이 안 보일 때 hand/pose 1초 10번 (인터페이스 정의 5.1)
STREAM_SIZE = (320, 240)     # 손 영상 (설계 9.2)
STREAM_PERIOD_S = 0.1        # 1초 10장
STREAM_JPEG_QUALITY = 70
CALIB_SAMPLE_FRAMES = 10     # [손 펴기 기록]/[주먹 기록] 때 평균 낼 최근 값 개수

# 맞춤 전 기본값 [엄지 거리 비율, 검지~새끼 두 마디 각도 합(°)]. 화면 표시용. 맞춤 전에는 calibrated=false라 관제가 쓰지 않음
DEFAULT_OPEN = [0.95, 350.0, 350.0, 350.0, 350.0]
DEFAULT_FIST = [0.35, 160.0, 160.0, 160.0, 160.0]

# 손가락별 관절 번호 (설계 9.2 그림): 뿌리, 가운데 두 마디, 끝
FINGER_JOINTS = {"index": (5, 6, 7, 8), "middle": (9, 10, 11, 12), "ring": (13, 14, 15, 16), "pinky": (17, 18, 19, 20)}

# 관절 점 연결선 (그리기용)
CONNECTIONS = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11),
               (11, 12), (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]


# ---------------------------------------------------------------------------
# 손 모양 계산 (설계 9.2) — 카메라 없이 시험할 수 있게 순수 함수로 둠
# ---------------------------------------------------------------------------

def angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """점 b에서 a와 c가 이루는 각도(도)."""
    v1, v2 = a - b, c - b
    n = float(np.linalg.norm(v1) * np.linalg.norm(v2))
    if n == 0.0:
        return 180.0
    return math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(v1, v2)) / n))))


def raw_values(p: np.ndarray) -> List[float]:
    """관절 점 21개 (21×3) → [엄지 거리 비율, 검지, 중지, 약지, 새끼 두 마디 각도 합].
    편 손이면 각도 합이 약 360°, 쥐면 작아짐. 엄지는 엄지 끝(4)~검지 뿌리(5) 거리 ÷ 손 크기(0~9 거리)."""
    size = float(np.linalg.norm(p[9] - p[0])) or 1e-6
    thumb = float(np.linalg.norm(p[4] - p[5])) / size
    bends = [angle(p[a], p[b], p[c]) + angle(p[b], p[c], p[d]) for a, b, c, d in FINGER_JOINTS.values()]
    return [thumb] + bends


def wrist_deg(p: np.ndarray, side: str) -> float:
    """손목 좌우 회전(°), −90~+90. 0 = 손바닥이 카메라를 봄, + = 조종자 기준 오른쪽으로 돌림.

    좌표는 MediaPipe 기준(x 오른쪽, y 아래, z 카메라에서 멀어지는 쪽), 거울로 뒤집은 사진 기준.
    cross(5−0, 17−0)은 오른손이면 손등 쪽, 왼손이면 손바닥 쪽을 가리키므로 오른손은 부호를 뒤집어
    항상 "손바닥이 향하는 방향"으로 맞춘다. 거울 사진에서는 조종자의 오른쪽이 사진의 오른쪽(+x)이다."""
    normal = np.cross(p[5] - p[0], p[17] - p[0])
    if side == "right":
        normal = -normal
    deg = math.degrees(math.atan2(float(normal[0]), float(-normal[2])))
    return max(-90.0, min(90.0, deg))


def to_ratio(values: Sequence[float], open_v: Sequence[float], fist_v: Sequence[float]) -> List[float]:
    """r = (편 값 − 지금 값) ÷ (편 값 − 쥔 값)을 0~1로 자름. 0 = 편 상태, 1 = 쥔 상태."""
    out = []
    for v, o, f in zip(values, open_v, fist_v):
        span = o - f
        r = 0.0 if abs(span) < 1e-6 else (o - v) / span
        out.append(round(max(0.0, min(1.0, r)), 3))
    return out


class Operator:
    """조종자 손 맞춤 (인터페이스 정의 5.2, 9.6 operator.json). open·fist 둘 다 있어야 calibrated."""

    def __init__(self, calib_dir: pathlib.Path = config.CALIB_DIR) -> None:
        self.calib_dir = calib_dir
        self.open: Optional[List[float]] = None
        self.fist: Optional[List[float]] = None
        saved = config.load_operator(calib_dir)
        if saved is not None:
            self.open, self.fist = list(saved["open"]), list(saved["fist"])

    @property
    def calibrated(self) -> bool:
        return self.open is not None and self.fist is not None

    def ratios(self, values: Sequence[float]) -> List[float]:
        return to_ratio(values, self.open or DEFAULT_OPEN, self.fist or DEFAULT_FIST)

    def apply(self, step: str, values: Optional[Sequence[float]]) -> str:
        """hand/calib 한 번. 손이 안 보이면(values None) open/fist는 무시. 결과 설명 문장을 돌려줌."""
        path = config.calibration_path("operator", self.calib_dir)
        if step == "reset":
            self.open = self.fist = None
            if path.exists():
                path.unlink()
            return "맞춤 지움"
        if values is None:
            return f"{step}: 손이 보이지 않아 무시함"
        rounded = [round(float(v), 3) for v in values]
        if step == "open":
            self.open = rounded
        else:
            self.fist = rounded
        if self.calibrated:
            self._save(path)
            return f"{step} 저장, 맞춤 완료"
        return f"{step} 저장, {'fist' if step == 'open' else 'open'}도 필요"

    def _save(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"measured": datetime.datetime.now().isoformat(timespec="seconds"),
                "open": self.open, "fist": self.fist}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


class Smoother:
    """최근 n개 값의 평균 (떨림 줄이기, 설계 9.2). 손을 놓치면 비움."""

    def __init__(self, n: int) -> None:
        self.buf: Deque[np.ndarray] = collections.deque(maxlen=n)

    def add(self, v: Sequence[float]) -> List[float]:
        self.buf.append(np.asarray(v, dtype=float))
        return [float(x) for x in np.mean(self.buf, axis=0)]

    def clear(self) -> None:
        self.buf.clear()


def pick_hand(handedness: Sequence[Any], image_lms: Sequence[Any], side: str, mirror: bool) -> Optional[int]:
    """따라 할 손 고르기 (설계 9.2). 고른 쪽 손이 여럿이면 사진에서 가장 큰 손.
    MediaPipe는 거울 사진을 기준으로 왼손·오른손을 구분하므로 mirror가 false면 반대 이름을 찾는다."""
    want = side.capitalize()
    if not mirror:
        want = "Left" if want == "Right" else "Right"
    best, best_area = None, -1.0
    for i, (cats, lms) in enumerate(zip(handedness, image_lms)):
        if not cats or cats[0].category_name != want:
            continue
        xs, ys = [p.x for p in lms], [p.y for p in lms]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys))
        if area > best_area:
            best, best_area = i, area
    return best


class FpsCounter:
    def __init__(self) -> None:
        self.times: Deque[float] = collections.deque()

    def tick(self, now: float) -> None:
        self.times.append(now)
        while self.times and now - self.times[0] > 1.0:
            self.times.popleft()

    def fps(self) -> float:
        return float(len(self.times))


class HandState:
    """사진 한 장의 MediaPipe 결과 → hand/pose 필드. 카메라·소켓과 분리해 시험할 수 있게 함."""

    def __init__(self, laptop: Dict[str, Any], operator: Operator) -> None:
        self.side = laptop["hand_side"]
        self.mirror = laptop["mirror"]
        self.operator = operator
        self.raw_smooth = Smoother(laptop["smooth_frames"])
        self.wrist_smooth = Smoother(laptop["smooth_frames"])
        self.recent_raw: Deque[List[float]] = collections.deque(maxlen=CALIB_SAMPLE_FRAMES)
        self.detected = False
        self.raw: Optional[List[float]] = None
        self.fingers: Optional[List[float]] = None
        self.wrist: Optional[float] = None
        self.image_points: Optional[List[Tuple[float, float]]] = None   # 그리기용 (0~1)

    def update(self, result: Any) -> None:
        idx = None
        if result is not None and result.hand_landmarks:
            idx = pick_hand(result.handedness, result.hand_landmarks, self.side, self.mirror)
        if idx is None:
            self.detected = False
            self.raw = self.fingers = self.wrist = self.image_points = None
            self.raw_smooth.clear()
            self.wrist_smooth.clear()
            self.recent_raw.clear()
            return
        # 각도는 실제 크기(미터) 좌표로 잰다: 사진 좌표는 가로·세로 축척이 달라 각도가 틀어짐
        world = np.array([[p.x, p.y, p.z] for p in result.hand_world_landmarks[idx]], dtype=float)
        self.image_points = [(p.x, p.y) for p in result.hand_landmarks[idx]]
        raw = raw_values(world)
        self.recent_raw.append(raw)
        self.raw = self.raw_smooth.add(raw)
        self.wrist = round(self.wrist_smooth.add([wrist_deg(world, self.side)])[0], 1)
        self.fingers = self.operator.ratios(self.raw)
        self.detected = True

    def calib_sample(self) -> Optional[List[float]]:
        """맞춤 때 쓸 값: 최근 몇 장의 평균. 손이 안 보이면 None."""
        if not self.detected or not self.recent_raw:
            return None
        return [float(x) for x in np.mean(self.recent_raw, axis=0)]

    def fields(self, fps: float) -> Dict[str, Any]:
        return {"detected": self.detected, "fingers": self.fingers, "wrist": self.wrist,
                "calibrated": self.operator.calibrated, "fps": round(fps, 1)}


# ---------------------------------------------------------------------------
# 그리기
# ---------------------------------------------------------------------------

def draw_hand(img: np.ndarray, pts: Optional[List[Tuple[float, float]]]) -> None:
    if not pts:
        return
    h, w = img.shape[:2]
    xy = [(int(x * w), int(y * h)) for x, y in pts]
    for a, b in CONNECTIONS:
        cv2.line(img, xy[a], xy[b], (255, 255, 255), 2)
    for p in xy:
        cv2.circle(img, p, 3, (0, 200, 0), -1)


def draw_debug(img: np.ndarray, st: HandState, fps: float) -> None:
    """--show 창 전용 글자."""
    lines = [f"fps {fps:.0f}  detected {st.detected}  calibrated {st.operator.calibrated}"]
    if st.detected:
        names = ["thumb", "index", "middle", "ring", "pinky"]
        lines.append("r: " + " ".join(f"{n[:2]}={r:.2f}" for n, r in zip(names, st.fingers or [])))
        lines.append(f"wrist {st.wrist:+.0f} deg")
        lines.append("raw: " + " ".join(f"{v:.2f}" if i == 0 else f"{v:.0f}" for i, v in enumerate(st.raw or [])))
    for i, text in enumerate(lines):
        cv2.putText(img, text, (8, 22 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
        cv2.putText(img, text, (8, 22 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def make_landmarker(max_hands: int) -> Any:
    from mediapipe.tasks.python import BaseOptions, vision
    if not MODEL_PATH.exists():
        print(f"[오류] 손 인식 모델 파일이 없음: {MODEL_PATH}", file=sys.stderr)
        sys.exit(1)
    opts = vision.HandLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(MODEL_PATH)),
                                        running_mode=vision.RunningMode.VIDEO, num_hands=max_hands)
    return vision.HandLandmarker.create_from_options(opts)


def open_source(camera: int, video: Optional[str]) -> cv2.VideoCapture:
    if video:
        cap = cv2.VideoCapture(video)
    else:
        cap = cv2.VideoCapture(camera, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(camera)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_SIZE[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_SIZE[1])
        cap.set(cv2.CAP_PROP_FPS, 30)
    if not cap.isOpened():
        print(f"[오류] {'영상 파일' if video else '웹캠'}을 열 수 없음: {video or camera}", file=sys.stderr)
        sys.exit(1)
    return cap


class CalibInbox:
    """hand/calib (UDP 5102)를 받는 스레드. 받은 step을 쌓아 두고 사진 처리 루프가 꺼내 씀."""

    def __init__(self, port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(0.5)
        self.steps: Deque[str] = collections.deque()
        self.seq = m.SeqFilter()
        self.running = True
        threading.Thread(target=self._loop, name="calib", daemon=True).start()

    def _loop(self) -> None:
        while self.running:
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                msg = m.decode(data)
            except m.MessageError as e:
                print(f"[무시] hand/calib 형식 오류: {e}")
                continue
            if msg["topic"] != m.HAND_CALIB:
                print(f"[무시] 모르는 topic: {msg['topic']}")
                continue
            if self.seq.accept(msg):
                self.steps.append(msg["step"])

    def close(self) -> None:
        self.running = False
        self.sock.close()


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="손 인식 서비스")
    ap.add_argument("--camera", type=int, help="웹캠 번호 (기본: laptop.yaml의 hand_camera_index)")
    ap.add_argument("--video", help="웹캠 대신 영상 파일")
    ap.add_argument("--show", action="store_true", help="확인용 창 띄우기 (q로 끝냄)")
    args = ap.parse_args(argv)

    net = config.or_exit(config.load_network)
    laptop = config.or_exit(config.load_laptop)
    try:
        operator = Operator()
    except config.ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        sys.exit(1)

    pose_addr = ("127.0.0.1", net["laptop"]["hand_pose_port"])
    out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out = m.Sender()
    inbox = CalibInbox(net["laptop"]["hand_calib_port"])
    video_server = MjpegServer(net["laptop"]["hand_video_port"])
    video_server.start()

    camera = args.camera if args.camera is not None else laptop["hand_camera_index"]
    cap = open_source(camera, args.video)
    landmarker = make_landmarker(laptop["hand_max_num"])
    state = HandState(laptop, operator)
    fps = FpsCounter()
    import mediapipe as mp

    print(f"손 인식 서비스 시작: {'영상 ' + args.video if args.video else '웹캠 ' + str(camera)}, "
          f"{laptop['hand_side']} 손, 맞춤 {'완료' if operator.calibrated else '필요'}")
    print(f"  hand/pose → udp {pose_addr[1]}, 손 영상 http://localhost:{net['laptop']['hand_video_port']}/video")

    last_lost_send = 0.0
    last_stream = 0.0
    last_ts_ms = -1
    t0 = time.monotonic()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                if args.video:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                print("[경고] 웹캠 사진을 못 읽음")
                time.sleep(0.1)
                continue
            now = time.monotonic()
            if laptop["mirror"]:
                frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            ts_ms = max(last_ts_ms + 1, int((now - t0) * 1000))   # VIDEO 모드는 시각이 계속 커져야 함
            last_ts_ms = ts_ms
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts_ms)
            state.update(result)
            fps.tick(now)

            while inbox.steps:
                step = inbox.steps.popleft()
                print(f"[손 맞춤] {operator.apply(step, state.calib_sample() if step != 'reset' else None)}")

            if state.detected or now - last_lost_send >= LOST_SEND_PERIOD_S:
                msg = out.make(m.HAND_POSE, **state.fields(fps.fps()))
                out_sock.sendto(m.encode(msg), pose_addr)
                if not state.detected:
                    last_lost_send = now

            if now - last_stream >= STREAM_PERIOD_S:
                last_stream = now
                small = cv2.resize(frame, STREAM_SIZE)
                draw_hand(small, state.image_points)
                ok_j, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY])
                if ok_j:
                    video_server.publish(jpg.tobytes())

            if args.show:
                view = frame.copy()
                draw_hand(view, state.image_points)
                draw_debug(view, state, fps.fps())
                cv2.imshow("hand_service (q: quit)", view)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        inbox.close()
        video_server.close()
        landmarker.close()
        if args.show:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
