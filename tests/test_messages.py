"""shared/messages.py 동작 시험 (만들기, 검사, 보내기·받기, 순번, 시리얼)."""

import json

import pytest

from shared import messages as m


def fixed_clock():
    return 1000.0


@pytest.fixture
def out():
    return m.Sender(clock=fixed_clock)


# --- 이름 ----------------------------------------------------------------------

def test_car_topic_roundtrip():
    assert m.car_topic(2, m.GOTO) == "car2/goto"
    assert m.split_car_topic("car2/goto") == (2, "goto")


@pytest.mark.parametrize("topic", ["car3/goto", "car1/fly", "carx/goto", "hand/pose", "car1"])
def test_split_car_topic_rejects(topic):
    assert m.split_car_topic(topic) is None


def test_car_topic_bad_input():
    with pytest.raises(ValueError):
        m.car_topic(3, m.GOTO)
    with pytest.raises(ValueError):
        m.car_topic(1, "fly")


# --- 만들기 ----------------------------------------------------------------------

def test_seq_counts_per_topic(out):
    a = out.make(m.car_topic(1, m.HEARTBEAT))
    b = out.make(m.car_topic(1, m.HEARTBEAT))
    c = out.make(m.car_topic(2, m.HEARTBEAT))
    assert (a["seq"], b["seq"], c["seq"]) == (0, 1, 0)
    assert a["t"] == 1000.0


def test_make_refuses_bad_message_and_keeps_seq(out):
    topic = m.car_topic(1, m.HAND)
    with pytest.raises(m.MessageError):
        out.make(topic, angles=[0, 0, 0, 0, 0, 181], ms=33)
    assert out.make(topic, angles=[0] * 6, ms=33)["seq"] == 0


def test_time_sync_offset():
    pi = m.Sender(clock=fixed_clock)
    pi.apply_time_sync(1005.5)
    assert pi.now() == 1005.5
    assert pi.make(m.car_topic(1, m.ARRIVED), station="wait_1")["t"] == 1005.5


# --- 검사 ----------------------------------------------------------------------

def hand(**over):
    msg = {"topic": "car1/hand", "seq": 1, "t": 1.0, "angles": [20, 120, 118, 121, 130, 90], "ms": 33}
    msg.update(over)
    return msg


def test_valid_hand():
    m.validate(hand())


@pytest.mark.parametrize("bad", [
    {"angles": [0] * 5},                 # 6개가 아님
    {"angles": [0, 0, 0, 0, 0, -1]},     # 범위 밖
    {"angles": [0, 0, 0, 0, 0, 1.5]},    # 정수가 아님
    {"angles": [0, 0, 0, 0, 0, True]},   # 불은 정수로 치지 않음
    {"ms": 1001},
    {"seq": -1},
    {"t": "now"},
])
def test_invalid_hand(bad):
    with pytest.raises(m.MessageError):
        m.validate(hand(**bad))


def test_missing_field():
    msg = hand()
    del msg["ms"]
    with pytest.raises(m.MessageError, match="ms"):
        m.validate(msg)


def test_unknown_field_is_ignored():
    m.validate(hand(extra="새 필드"))


def test_unknown_topic():
    with pytest.raises(m.UnknownTopic):
        m.validate({"topic": "car1/fly", "seq": 0, "t": 1.0})
    with pytest.raises(m.UnknownTopic):
        m.validate({"topic": "music/play", "seq": 0, "t": 1.0})


def test_optional_field_checked_when_present():
    m.validate({"topic": "car1/stop", "seq": 0, "t": 1.0})
    m.validate({"topic": "car1/stop", "seq": 0, "t": 1.0, "reason": "space"})
    with pytest.raises(m.MessageError):
        m.validate({"topic": "car1/stop", "seq": 0, "t": 1.0, "reason": 3})


def test_hand_pose_lost_hand():
    m.validate({"topic": "hand/pose", "seq": 0, "t": 1.0, "detected": False,
                "fingers": None, "wrist": None, "calibrated": True, "fps": 10.0})


def test_vision_slots_need_all_three():
    base = {"topic": "vision/state", "seq": 0, "t": 1.0, "bench": "empty", "cock_led": None, "fps": 30.0}
    m.validate(dict(base, slots={"1": "empty", "2": "occupied", "3": "unknown"}))
    with pytest.raises(m.MessageError):
        m.validate(dict(base, slots={"1": "empty", "2": "occupied"}))


@pytest.mark.parametrize("cmd, ok", [
    ({"action": "stop_all"}, True),
    ({"action": "start", "car": 1, "task": "a.json", "repeat": True}, True),
    ({"action": "start", "car": 1, "task": "a.json"}, False),      # repeat 없음
    ({"action": "set_mode", "car": 1, "mode": "auto"}, False),     # auto는 [시작]으로만
    ({"action": "select_car", "car": 3}, False),
    ({"action": "dance"}, False),
])
def test_ui_cmd(cmd, ok):
    msg = dict({"topic": "ui/cmd", "seq": 0, "t": 1.0}, **cmd)
    if ok:
        m.validate(msg)
    else:
        with pytest.raises(m.MessageError):
            m.validate(msg)


# --- 보내기·받기 ----------------------------------------------------------------------

def test_encode_decode_roundtrip(out):
    msg = out.make(m.UI_ALERT, level="info", car=None, code="task_done", text="작업이 끝났습니다")
    raw = m.encode(msg)
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert "작업" in raw.decode("utf-8")          # 한글을 \uXXXX로 바꾸지 않음
    assert m.decode(raw) == msg


@pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe", b"[1,2]", b'{"seq":1}'])
def test_decode_garbage(raw):
    with pytest.raises(m.MessageError):
        m.decode(raw)


def test_line_reader_handles_split_and_joined_chunks(out):
    a = m.encode(out.make(m.car_topic(1, m.HEARTBEAT)))
    b = m.encode(out.make(m.car_topic(1, m.GOTO), station="cock"))
    stream = a + b
    reader = m.LineReader()
    got = []
    for chunk in (stream[:5], stream[5:len(a) + 3], stream[len(a) + 3:]):
        got.extend(m.decode(line) for line in reader.feed(chunk))
    assert [g["topic"] for g in got] == ["car1/heartbeat", "car1/goto"]


def test_line_reader_skips_blank_lines():
    assert list(m.LineReader().feed(b"\n\n")) == []


# --- UDP 순번 ----------------------------------------------------------------------

def test_seq_filter():
    f = m.SeqFilter()
    msg = lambda s: {"topic": "car1/hand", "seq": s}
    assert f.accept(msg(10))
    assert not f.accept(msg(10))      # 같은 순번
    assert not f.accept(msg(9))       # 늦게 온 옛 값
    assert f.accept(msg(11))
    assert f.accept({"topic": "car2/hand", "seq": 0})   # topic마다 따로


def test_seq_filter_sender_restart():
    f = m.SeqFilter()
    assert f.accept({"topic": "hand/pose", "seq": 5000})
    assert not f.accept({"topic": "hand/pose", "seq": 4000})   # 1000 차이: 옛 값
    assert f.accept({"topic": "hand/pose", "seq": 0})          # 1000 넘게 작음: 다시 켜짐


# --- 파이 상태별 명령 ----------------------------------------------------------------------

def test_pi_policy_examples():
    assert m.pi_policy(m.HAND, "arrived") == m.ACCEPT
    assert m.pi_policy(m.HAND, "driving") == m.DROP
    assert m.pi_policy(m.GOTO, "driving") == m.REJECT_CMD
    assert m.pi_policy(m.STOP, "error") == m.ACCEPT


# --- 시리얼 ----------------------------------------------------------------------

def test_serial_commands():
    assert m.serial_ping() == b"P\n"
    assert m.serial_move([20, 120, 118, 121, 130, 90], 33) == b"M 20 120 118 121 130 90 33\n"
    assert m.serial_servo(6, 90, 300) == b"S 6 90 300\n"


@pytest.mark.parametrize("angles, ms", [([0] * 5, 33), ([0] * 5 + [200], 33), ([0] * 6, 2000)])
def test_serial_move_rejects(angles, ms):
    with pytest.raises(m.MessageError):
        m.serial_move(angles, ms)


def test_serial_reply():
    assert m.parse_serial_reply(b"OK\r\n") == (True, "")
    assert m.parse_serial_reply("ERR range") == (False, "range")
    with pytest.raises(m.MessageError):
        m.parse_serial_reply(b"A12$")
