"""손 인식 서비스 시험: 웹캠·MediaPipe 없이 가짜 관절 점으로 손 모양 계산을 확인 (설계 9.2, 인터페이스 정의 5.1·5.2)."""

import math
import socket
import types

import numpy as np
import pytest

from laptop.hand_service import main as hs
from shared import config
from shared import messages as m


def right_hand(curl: float = 0.0) -> np.ndarray:
    """거울 사진 기준 오른손, 손바닥이 카메라를 봄 (검지가 사진 왼쪽). curl 0 = 편 손, 1 = 주먹.
    좌표: x 오른쪽, y 아래, z 카메라에서 멀어지는 쪽 (MediaPipe와 같음). 단위 m."""
    p = np.zeros((21, 3))
    p[0] = [0.0, 0.09, 0.0]
    bases = {5: -0.03, 9: -0.01, 13: 0.01, 17: 0.03}
    seg = 0.025
    for base, x in bases.items():
        p[base] = [x, 0.0, 0.0]
        # 마디가 손바닥 쪽(카메라 쪽, −z)으로 굽음. 마디마다 curl × 90°씩 꺾임
        direction = np.array([0.0, -1.0, 0.0])
        pos = p[base].copy()
        for k in range(1, 4):
            a = math.radians(90 * curl) * k
            direction = np.array([0.0, -math.cos(a), -math.sin(a)])
            pos = pos + seg * direction
            p[base + k] = pos
    # 엄지: 편 손이면 검지 뿌리에서 멀리, 주먹이면 검지 뿌리 가까이
    p[1] = [-0.03, 0.07, 0.0]
    p[2] = [-0.05, 0.05, 0.0]
    p[3] = [-0.06, 0.03, 0.0]
    p[4] = [-0.03 - 0.05 * (1 - curl), 0.0 + 0.01 * (1 - curl), -0.01 * curl]
    return p


def rotate_y(p: np.ndarray, deg: float) -> np.ndarray:
    """세로축으로 돌림. 손바닥 방향 (0,0,−1)이 (sin θ, 0, −cos θ)가 되도록 (θ>0 = 조종자 오른쪽)."""
    phi = math.radians(-deg)
    r = np.array([[math.cos(phi), 0, math.sin(phi)], [0, 1, 0], [-math.sin(phi), 0, math.cos(phi)]])
    return (p - p[0]) @ r.T + p[0]


def left_of(p: np.ndarray) -> np.ndarray:
    q = p.copy()
    q[:, 0] *= -1
    return q


# --- 굽힘 ---------------------------------------------------------------

def test_open_hand_bend_near_360():
    raw = hs.raw_values(right_hand(0.0))
    assert all(v > 350 for v in raw[1:])


def test_fist_bend_much_smaller():
    open_raw, fist_raw = hs.raw_values(right_hand(0.0)), hs.raw_values(right_hand(1.0))
    for o, f in zip(open_raw, fist_raw):
        assert f < o
    assert all(v < 200 for v in fist_raw[1:])


def test_ratio_after_calibration():
    open_raw, fist_raw = hs.raw_values(right_hand(0.0)), hs.raw_values(right_hand(1.0))
    half = hs.raw_values(right_hand(0.5))
    assert hs.to_ratio(open_raw, open_raw, fist_raw) == [0.0] * 5
    assert hs.to_ratio(fist_raw, open_raw, fist_raw) == [1.0] * 5
    assert all(0.2 < r < 0.8 for r in hs.to_ratio(half, open_raw, fist_raw))


def test_ratio_clamped_and_safe():
    assert hs.to_ratio([400, 100], [350, 350], [150, 150]) == [0.0, 1.0]
    assert hs.to_ratio([200], [200], [200]) == [0.0]          # 편 값 = 쥔 값이어도 0으로 나누지 않음


def test_angle_degenerate_points():
    z = np.zeros(3)
    assert hs.angle(z, z, np.ones(3)) == 180.0


# --- 손목 ---------------------------------------------------------------

@pytest.mark.parametrize("deg", [0, 30, -45, 80])
def test_wrist_right_hand(deg):
    assert hs.wrist_deg(rotate_y(right_hand(), deg), "right") == pytest.approx(deg, abs=1.0)


@pytest.mark.parametrize("deg", [0, 30, -45])
def test_wrist_left_hand(deg):
    # 왼손: 오른손의 거울 모양. 조종자 오른쪽으로 돌리면 +
    assert hs.wrist_deg(rotate_y(left_of(right_hand()), deg), "left") == pytest.approx(deg, abs=1.0)


def test_wrist_clamped():
    assert hs.wrist_deg(rotate_y(right_hand(), 120), "right") == 90.0


# --- 손 고르기 --------------------------------------------------------------

def _cat(name):
    return [types.SimpleNamespace(category_name=name)]


def _lms(x0, size):
    return [types.SimpleNamespace(x=x0 + size * (i % 2), y=0.1 + size * (i % 3) / 2) for i in range(21)]


def test_pick_hand_side_and_biggest():
    hd = [_cat("Left"), _cat("Right"), _cat("Right")]
    lms = [_lms(0.1, 0.5), _lms(0.1, 0.1), _lms(0.5, 0.3)]
    assert hs.pick_hand(hd, lms, "right", mirror=True) == 2
    assert hs.pick_hand(hd, lms, "left", mirror=True) == 0
    assert hs.pick_hand(hd, lms, "right", mirror=False) == 0     # 거울이 아니면 이름이 반대
    assert hs.pick_hand([_cat("Left")], [_lms(0, 0.1)], "right", mirror=True) is None


# --- 조종자 맞춤 + hand/pose 필드 --------------------------------------------

LAPTOP = {"hand_side": "right", "mirror": True, "smooth_frames": 1, "hand_camera_index": 0, "hand_max_num": 2}


def _result(points):
    lm = [types.SimpleNamespace(x=0.5 + p[0], y=0.5 + p[1], z=p[2]) for p in points]
    return types.SimpleNamespace(handedness=[_cat("Right")], hand_landmarks=[lm], hand_world_landmarks=[lm])


def test_operator_calibration_saved_and_reloaded(tmp_path):
    op = hs.Operator(tmp_path)
    st = hs.HandState(LAPTOP, op)
    assert not op.calibrated
    assert "무시" in op.apply("open", None)                       # 손이 안 보이면 무시

    st.update(_result(right_hand(0.0)))
    op.apply("open", st.calib_sample())
    assert not op.calibrated
    st.update(_result(right_hand(1.0)))
    for _ in range(hs.CALIB_SAMPLE_FRAMES):
        st.update(_result(right_hand(1.0)))
    op.apply("fist", st.calib_sample())
    assert op.calibrated
    saved = config.load_operator(tmp_path)                      # shared/config 검사를 통과하는 형식
    assert saved is not None and len(saved["open"]) == 5

    assert hs.Operator(tmp_path).calibrated                     # 다시 켜도 유지
    op.apply("reset", None)
    assert not op.calibrated and config.load_operator(tmp_path) is None


def test_hand_state_fields_are_valid_message(tmp_path):
    op = hs.Operator(tmp_path)
    st = hs.HandState(LAPTOP, op)
    st.update(_result(right_hand(0.0)))
    op.apply("open", st.calib_sample())
    st.update(_result(right_hand(1.0)))
    op.apply("fist", st.calib_sample())
    st.update(_result(right_hand(1.0)))
    msg = m.Sender().make(m.HAND_POSE, **st.fields(29.6))      # make()가 인터페이스 정의대로 검사
    assert msg["detected"] and msg["calibrated"]
    assert msg["fingers"] == [1.0] * 5

    st.update(types.SimpleNamespace(handedness=[], hand_landmarks=[], hand_world_landmarks=[]))
    msg = m.Sender().make(m.HAND_POSE, **st.fields(10.0))
    assert msg["detected"] is False and msg["fingers"] is None and msg["wrist"] is None
    assert st.calib_sample() is None


def test_calib_inbox_receives_steps():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    inbox = hs.CalibInbox(port)
    try:
        out = m.Sender()
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for step in ("open", "fist"):
            tx.sendto(m.encode(out.make(m.HAND_CALIB, step=step)), ("127.0.0.1", port))
        tx.sendto(b"not json", ("127.0.0.1", port))
        import time
        deadline = time.time() + 2
        while len(inbox.steps) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert list(inbox.steps) == ["open", "fist"]
    finally:
        inbox.close()
