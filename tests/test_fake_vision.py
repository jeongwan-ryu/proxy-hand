"""가짜 맵 비전 시험: 보내는 vision/state가 약속(인터페이스 정의 5.3)에 맞고, 콘솔 명령대로 바뀌는지."""

import socket

import pytest

from shared import messages as m
from tests.fakes.fake_vision import FakeVision, handle_command


@pytest.fixture
def rig():
    recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv.bind(("127.0.0.1", 0))
    recv.settimeout(1.0)
    fv = FakeVision(recv.getsockname())
    yield fv, recv
    fv.close()
    recv.close()


def receive(recv):
    return m.decode(recv.recv(4096))          # 형식이 틀리면 여기서 MessageError


def test_starts_with_full_storage_empty_bench_led_off(rig):
    fv, recv = rig
    fv.send_once()
    msg = receive(recv)
    assert msg["topic"] == m.VISION_STATE
    assert msg["slots"] == {"1": "occupied", "2": "occupied", "3": "occupied"}
    assert msg["bench"] == "empty" and msg["cock_led"] is False


def test_console_changes_values(rig, capsys):
    fv, recv = rig
    handle_command(fv, "2")                    # 칸 2 비움 (바이알을 집음)
    handle_command(fv, "b")                    # 작업대에 놓음
    handle_command(fv, "led")
    handle_command(fv, "3 ?")
    fv.send_once()
    msg = receive(recv)
    assert msg["slots"] == {"1": "occupied", "2": "empty", "3": "unknown"}
    assert msg["bench"] == "occupied" and msg["cock_led"] is True
    handle_command(fv, "3")                    # 모름 → 있음
    handle_command(fv, "b ?")
    handle_command(fv, "led none")
    fv.send_once()
    msg = receive(recv)
    assert msg["slots"]["3"] == "occupied" and msg["bench"] == "unknown" and msg["cock_led"] is None
    assert "칸2=없음" in capsys.readouterr().out


def test_off_stops_sending_and_on_resumes(rig):
    fv, recv = rig
    fv.start()
    receive(recv)                              # 1초 10번으로 오는 중
    handle_command(fv, "off")
    recv.settimeout(0.05)
    try:
        while True:
            recv.recv(4096)                    # 멈추기 전에 보낸 것 비우기
    except socket.timeout:
        pass
    recv.settimeout(0.5)
    with pytest.raises(socket.timeout):
        recv.recv(4096)
    handle_command(fv, "on")
    recv.settimeout(1.0)
    assert receive(recv)["topic"] == m.VISION_STATE


def test_unknown_command_and_quit(rig, capsys):
    fv, _ = rig
    assert handle_command(fv, "zzz")
    assert "명령:" in capsys.readouterr().out
    assert handle_command(fv, "")
    assert not handle_command(fv, "quit")
