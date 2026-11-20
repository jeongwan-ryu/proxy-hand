"""가짜 작업차 시험: 가짜 선로가 진짜 작업차 서비스(CarCore + Service)를 제대로 움직이는지.

가짜 관제(FakeControl)로 TCP 명령을 보내고 보고를 받는다. 정지선 사이 0.5초, 센서 200Hz로 빨리 돌림.
"""

import time

import pytest

from shared import config
from shared import messages as m
from tests.fakes.fake_car import FakeCar, FakeTrack, handle_command, map_config
from tests.fakes.fake_control import FakeControl
from tests.fakes.fake_line import CENTER, NOTHING, STOPLINE

SEGMENT = 0.5
TIMING = config.load_network()["timing"]
MP = map_config(SEGMENT)
WAIT = 5.0


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


# --- 가짜 선로만 ------------------------------------------------------------------

def test_track_sensors_follow_position():
    clock = Clock()
    track = FakeTrack(6, 2.0, clock)
    assert track.read_sensors() == STOPLINE              # 처음엔 대기 정거장 정지선 위
    track.drive(100, 100)                                # 보통 속도: 정지선 사이 2초
    clock.t += 1.0
    assert track.read_sensors() == CENTER
    clock.t += 1.0
    assert track.read_sensors() == STOPLINE and track.line_under() == 1
    track.drive(0, 0)
    clock.t += 5.0
    assert track.line_under() == 1                       # 멈추면 그 자리


def test_track_place_wraps_lost_and_miss():
    clock = Clock()
    track = FakeTrack(3, 1.0, clock)
    track.place(2)
    track.drive(100, 100)
    clock.t += 1.0
    assert track.read_sensors() == STOPLINE and track.line_under() == 0   # 고리를 한 바퀴 돎
    track.lose_line()
    assert track.read_sensors() == NOTHING
    clock.t += 1.2                                       # 위치 1.25, 선이 다시 보임
    assert track.read_sensors() == CENTER
    track.miss_next()
    clock.t += 0.8                                       # 위치 2.05
    assert track.line_under() == 2 and track.read_sensors() == CENTER    # 숨긴 정지선
    clock.t += 1.0                                       # 위치 0.05
    assert track.read_sensors() == STOPLINE              # 그다음 정지선은 보임


# --- 진짜 작업차 서비스 + 가짜 선로 -----------------------------------------------------

@pytest.fixture
def rig():
    made = []

    def make(car_id=1):
        car = FakeCar(car_id, MP, TIMING, cmd_port=0, hand_port=0, segment_s=SEGMENT, sensor_hz=200)
        car.start()
        ctl = FakeControl("127.0.0.1", car.ports[0], car=car_id)
        ctl.start_heartbeat(0.05)
        made.append((car, ctl))
        return car, ctl

    yield make
    for car, ctl in made:
        ctl.close()
        car.close()


def test_goto_arrives_at_right_stopline(rig):
    car, ctl = rig(1)
    ctl.send(m.SET_STATION, station="wait_1")
    ctl.wait_for(m.ARRIVED, station="wait_1")
    ctl.send(m.GOTO, station="storage_2")
    ctl.wait_for(m.STATUS, state="driving")
    ctl.wait_for(m.ARRIVED, timeout=WAIT, station="storage_2")
    assert car.track.line_under() == 2                   # 선로 위에서도 2번 정지선에 섬
    assert car.track.speed == 0


def test_set_station_places_car_and_goto_wraps_loop(rig):
    car, ctl = rig(1)
    ctl.send(m.SET_STATION, station="cock")              # 5번 정지선에 놓음
    ctl.wait_for(m.ARRIVED, station="cock")
    assert car.track.line_under() == 5
    ctl.send(m.GOTO, station="wait_1")                   # 5 → 0: 고리를 넘어감
    ctl.wait_for(m.ARRIVED, timeout=WAIT, station="wait_1")
    assert car.track.line_under() == 0


def test_car2_measure_loop(rig):
    car, ctl = rig(2)
    ctl.send(m.SET_STATION, station="wait_2")
    ctl.wait_for(m.ARRIVED, station="wait_2")
    ctl.send(m.GOTO, station="meter")
    ctl.wait_for(m.ARRIVED, timeout=WAIT, station="meter")
    assert car.track.line_under() == 2


def test_lost_gives_line_lost(rig):
    car, ctl = rig(1)
    ctl.send(m.SET_STATION, station="wait_1")
    ctl.send(m.GOTO, station="bench_1")
    ctl.wait_for(m.STATUS, state="driving")
    car.track.lose_line()
    assert ctl.wait_for(m.ERROR, code="line_lost")
    ctl.wait_for(m.STATUS, state="error")


def test_miss_gives_line_missed(rig):
    car, ctl = rig(1)
    ctl.send(m.SET_STATION, station="wait_1")
    ctl.send(m.GOTO, station="bench_1")
    ctl.wait_for(m.STATUS, state="driving")
    car.track.miss_next()
    assert ctl.wait_for(m.ERROR, timeout=WAIT, code="line_missed")


def test_serial_off_then_on(rig):
    car, ctl = rig(1)
    ctl.send(m.SET_STATION, station="wait_1")
    ctl.wait_for(m.ARRIVED, station="wait_1")
    assert car.set_serial(False)
    ctl.send_hand(car.ports[1], [90] * 6, 33)            # 손 명령 3번 응답 없음 → serial_fail
    for _ in range(3):
        time.sleep(0.05)
        ctl.send_hand(car.ports[1], [90] * 6, 33)
    assert ctl.wait_for(m.ERROR, code="serial_fail")
    car.set_serial(True)
    assert ctl.wait_for(m.STATUS, timeout=WAIT, serial_ok=True)   # 1초 뒤 다시 열고 P에 OK


def test_drop_closes_link(rig):
    car, ctl = rig(1)
    ctl.wait_for(m.STATUS)
    assert car.drop()
    end = time.monotonic() + 2
    while car.service._client is not None and time.monotonic() < end:
        time.sleep(0.01)
    assert car.service._client is None


def test_console_commands(rig, capsys):
    car, ctl = rig(1)
    cars = {1: car}
    assert handle_command(cars, "where")
    assert "작업차 1" in capsys.readouterr().out
    assert handle_command(cars, "miss 1") and car.track.hide_next
    assert handle_command(cars, "serial 1 off") and car.serial.mode == "silent"
    assert handle_command(cars, "foo 9")
    assert "명령:" in capsys.readouterr().out
    assert not handle_command(cars, "quit")
