"""AiHand 보정 도구 시험 (하드웨어 없이, 가짜 micro:bit FakeSerial 사용)."""

import json

import pytest

from laptop.tools import aihand_calib as ac
from shared import config
from tests.fakes.fake_serial import FakeSerial


def make_session(tmp_path, link=None, name="aihand_A"):
    return ac.Session(ac.default_data(name), link, calib_dir=tmp_path)


def test_default_data_passes_config_check(tmp_path):
    path = ac.save(ac.default_data("aihand_A"), tmp_path)
    d = config.load_aihand("aihand_A", tmp_path)
    assert path.name == "aihand_A.json"
    assert d["servos"][1] == {"id": 2, "part": "index", "open": 150, "closed": 32}
    assert d["servos"][5]["center"] == 90
    assert d["drive_wrist"] == 90
    assert d["margin_deg"] == 5


def test_copy_changes_only_name(tmp_path):
    ac.save(ac.default_data("aihand_A"), tmp_path)
    b = ac.prepare("aihand_B", new=False, copy_from="aihand_A", calib_dir=tmp_path)
    a = config.load_aihand("aihand_A", tmp_path)
    assert b["name"] == "aihand_B"
    assert b["servos"] == a["servos"] and b["drive_wrist"] == a["drive_wrist"]


def test_prepare_does_not_overwrite_existing(tmp_path):
    ac.save(ac.default_data("aihand_A"), tmp_path)
    with pytest.raises(ValueError):
        ac.prepare("aihand_A", new=True, copy_from=None, calib_dir=tmp_path)
    ac.save(ac.default_data("aihand_B"), tmp_path)
    with pytest.raises(ValueError):
        ac.prepare("aihand_B", new=False, copy_from="aihand_A", calib_dir=tmp_path)


def test_prepare_missing_files(tmp_path):
    with pytest.raises(ValueError):
        ac.prepare("aihand_A", new=False, copy_from=None, calib_dir=tmp_path)
    with pytest.raises(ValueError):
        ac.prepare("aihand_B", new=False, copy_from="aihand_A", calib_dir=tmp_path)


def test_try_sends_saved_angle(tmp_path):
    link = FakeSerial()
    s = make_session(tmp_path, link)
    assert "OK" in s.handle("try 2 closed")
    assert link.sent == [b"S 2 32 500\n"]
    assert not s.dirty


def test_set_changes_value_and_sends(tmp_path):
    link = FakeSerial()
    s = make_session(tmp_path, link)
    s.handle("set 2 closed 35")
    assert s.data["servos"][1]["closed"] == 35
    assert link.sent == [b"S 2 35 500\n"]
    assert s.dirty


def test_center_and_drive_move_wrist(tmp_path):
    link = FakeSerial()
    s = make_session(tmp_path, link)
    s.handle("center 95")
    s.handle("drive 10")
    assert s.data["servos"][5]["center"] == 95
    assert s.data["drive_wrist"] == 10
    assert link.sent == [b"S 6 95 500\n", b"S 6 10 500\n"]


@pytest.mark.parametrize("line", [
    "set 2 closed 181", "set 2 closed -1", "set 6 open 90", "set 0 open 90",
    "try 2 middle", "center 200", "drive abc", "set 2 closed x",
])
def test_bad_input_sends_nothing_and_changes_nothing(tmp_path, line):
    link = FakeSerial()
    s = make_session(tmp_path, link)
    before = json.dumps(s.data, sort_keys=True)
    out = s.handle(line)
    assert out
    assert link.sent == []
    assert json.dumps(s.data, sort_keys=True) == before


def test_save_refuses_values_that_fail_check(tmp_path):
    s = make_session(tmp_path)
    s.handle("set 2 open 150")
    s.handle("set 2 closed 145")       # 차이 5 <= margin 5 x 2
    out = s.handle("save")
    assert out.startswith("저장 안 함")
    assert not config.calibration_path("aihand_A", tmp_path).exists()
    assert s.dirty


def test_save_refuses_drive_wrist_outside_range(tmp_path):
    d = ac.default_data("aihand_A")
    d["servos"][5]["max"] = 150
    d["drive_wrist"] = 170
    with pytest.raises(config.ConfigError):
        ac.save(d, tmp_path)


def test_save_after_fix(tmp_path):
    s = make_session(tmp_path)
    s.handle("set 3 closed 34")
    assert s.handle("save").startswith("저장함")
    assert not s.dirty
    assert config.load_aihand("aihand_A", tmp_path)["servos"][2]["closed"] == 34


def test_no_link_does_not_send(tmp_path):
    s = make_session(tmp_path, link=None)
    out = s.handle("set 2 closed 35")
    assert "보내지 않음" in out
    assert s.data["servos"][1]["closed"] == 35


def test_no_reply_is_reported(tmp_path):
    s = make_session(tmp_path, FakeSerial(mode="silent"))
    assert "응답 없음" in s.handle("try 1 open")
