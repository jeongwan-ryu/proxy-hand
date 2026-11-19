"""맵 비전 서비스 시험: OpenCV로 만든 ArUco 마커 그림으로 가려짐·콕 LED 판단 (웹캠 없이)."""

import socket
import threading

import cv2
import numpy as np
import pytest

from laptop.vision_service import main as vs
from shared import config
from shared import messages as m

MP = config.load_map()
TOPCAM = {"camera_index": 1, "width": 1920, "height": 1080,
          "cock_led": {"rect": [1700, 900, 24, 24], "on_threshold": 128.0}}
MARKER_PX = 64       # 시스템 설계 5.2 예: 40mm 마커 → 64픽셀
POS = {1: (300, 300), 2: (600, 300), 3: (900, 300), 4: (1300, 600)}


def scene(hidden=(), led_on=False):
    """1920×1080 흰 바닥에 마커 1~4. hidden에 있는 마커는 회색 원(바이알)으로 가림."""
    img = np.full((1080, 1920, 3), 200, np.uint8)
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for mid, (x, y) in POS.items():
        mk = cv2.aruco.generateImageMarker(d, mid, MARKER_PX)
        img[y - 10:y + MARKER_PX + 10, x - 10:x + MARKER_PX + 10] = 255   # 흰 테두리
        img[y:y + MARKER_PX, x:x + MARKER_PX] = cv2.cvtColor(mk, cv2.COLOR_GRAY2BGR)
        if mid in hidden:
            cv2.circle(img, (x + MARKER_PX // 2, y + MARKER_PX // 2), 50, (90, 90, 90), -1)
    x, y, w, h = TOPCAM["cock_led"]["rect"]
    img[y:y + h, x:x + w] = 250 if led_on else 40
    return img


def run(vision, img, t0, seconds, step=1 / 30):
    t = t0
    while t < t0 + seconds:
        vision.process(img, t)
        t += step
    return t


def test_detects_all_markers_at_64px():
    v = vs.Vision(MP, TOPCAM)
    v.process(scene(), 0.0)
    assert v.seen == {1, 2, 3, 4}


def test_unknown_until_half_second_then_decides():
    v = vs.Vision(MP, TOPCAM)
    img = scene(hidden=(2, 4))
    run(v, img, 0.0, 0.4)
    assert v.fields()["slots"] == {"1": "unknown", "2": "unknown", "3": "unknown"}
    run(v, img, 0.4, 0.3)
    f = v.fields()
    assert f["slots"] == {"1": "empty", "2": "occupied", "3": "empty"}
    assert f["bench"] == "occupied"


def test_short_flicker_keeps_previous_value():
    v = vs.Vision(MP, TOPCAM)
    t = run(v, scene(), 0.0, 1.0)
    t = run(v, scene(hidden=(1,)), t, 0.3)          # 0.3초만 가려짐 (손이 스침, 인식 놓침)
    assert v.fields()["slots"]["1"] == "empty"
    run(v, scene(hidden=(1,)), t, 0.6)              # 계속 가려짐 → 있음
    assert v.fields()["slots"]["1"] == "occupied"


def test_led_needs_0_3s_and_turns_off_at_once():
    v = vs.Vision(MP, TOPCAM)
    t = run(v, scene(led_on=True), 0.0, 0.2)
    assert v.fields()["cock_led"] is False          # 0.3초 안 됨 (반사광일 수 있음)
    t = run(v, scene(led_on=True), t, 0.2)
    assert v.fields()["cock_led"] is True
    v.process(scene(), t)
    assert v.fields()["cock_led"] is False


def test_no_topcam_gives_null_led():
    v = vs.Vision(MP, None)
    v.process(scene(led_on=True), 0.0)
    assert v.fields()["cock_led"] is None


def test_fields_make_valid_message():
    v = vs.Vision(MP, TOPCAM)
    run(v, scene(hidden=(3,)), 0.0, 0.6)
    msg = m.Sender().make(m.VISION_STATE, fps=29.8, **v.fields())
    assert m.decode(m.encode(msg))["slots"]["3"] == "occupied"


def test_draw_keeps_size():
    v = vs.Vision(MP, TOPCAM)
    img = scene()
    v.process(img, 0.0)
    assert v.draw(img).shape == img.shape


def test_main_sends_vision_state_from_video_file(tmp_path, monkeypatch):
    """녹화 파일 → main() → UDP로 vision/state가 오는지 (관제 자리에서 받아 봄)."""
    path = str(tmp_path / "map.avi")
    wr = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 30, (1920, 1080))
    for _ in range(10):
        wr.write(scene(hidden=(2,)))
    wr.release()

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(5)
    net = config.load_network()
    net["laptop"]["vision_state_port"] = rx.getsockname()[1]
    net["laptop"]["map_video_port"] = 0
    monkeypatch.setattr(config, "load_network", lambda: net)
    monkeypatch.setattr(config, "load_topcam", lambda: TOPCAM)

    threading.Thread(target=vs.main, args=(["--video", path],), daemon=True).start()
    msgs = []
    while not msgs or msgs[-1]["slots"]["2"] != "occupied":
        msgs.append(m.decode(rx.recv(4096)))
        assert len(msgs) < 50
    assert msgs[0]["topic"] == m.VISION_STATE
    assert msgs[-1]["slots"]["1"] == "empty"


@pytest.mark.parametrize("bad", ["DICT_9X9_1"])
def test_bad_dictionary_is_config_error(bad):
    mk = dict(MP.markers, dictionary=bad)
    with pytest.raises(config.ConfigError):
        vs.make_detector(mk)
