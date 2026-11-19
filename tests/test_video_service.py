"""영상 서비스 시험: 가짜 카메라 → JPEG가 서버로 가는지, 카메라가 빠지면 다시 여는지 (파이·카메라 없이)."""

import numpy as np

from pi.video_service import main as vs
from shared import config

VIDEO = config.load_car(require_tuned=False)["video"]


class FakeCap:
    def __init__(self, frames):
        self.frames = list(frames)

    def read(self):
        if not self.frames:
            return False, None
        return True, self.frames.pop(0)

    def release(self):
        pass


class FakeServer:
    def __init__(self):
        self.jpegs = []

    def publish(self, jpeg):
        self.jpegs.append(jpeg)


def test_frames_become_jpeg_and_camera_reopens():
    img = np.zeros((VIDEO["height"], VIDEO["width"], 3), np.uint8)
    caps = [FakeCap([img, img]), FakeCap([img])]         # 첫 카메라는 2장 뒤 빠짐 → 다시 엶
    opened, slept = [], []
    t = [0.0]

    def factory(video):
        opened.append(1)
        return caps.pop(0) if caps else FakeCap([])

    def clock():
        t[0] += 1.0                                      # 사진마다 1초 → fps 제한에 안 걸림
        return t[0]

    server = FakeServer()
    vs.run(VIDEO, server, cap_factory=factory, should_stop=lambda: len(opened) > 2,
           clock=clock, sleep=slept.append)
    assert len(server.jpegs) == 3
    assert all(j[:2] == b"\xff\xd8" for j in server.jpegs)   # JPEG 시작 표시
    assert slept == [vs.REOPEN_S, vs.REOPEN_S]


def test_fps_limit_drops_extra_frames():
    img = np.zeros((VIDEO["height"], VIDEO["width"], 3), np.uint8)
    server = FakeServer()
    cap = FakeCap([img] * 10)
    vs.run(VIDEO, server, cap_factory=lambda v: cap, should_stop=lambda: not cap.frames,
           clock=lambda: 0.0, sleep=lambda s: None)
    assert len(server.jpegs) == 1                        # 시각이 그대로면 첫 장만
