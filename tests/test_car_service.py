"""작업차 서비스 시험 (파이·Raspbot·micro:bit 없이).

앞부분: CarCore·LineTracker에 시각과 센서 값을 직접 넣어 규칙을 봄 (인터페이스 정의 4.4, 시스템 설계 10장).
끝부분: Service를 실제 TCP/UDP로 띄우고 가짜 관제·가짜 선 센서·가짜 micro:bit로 한 바퀴.
"""

import copy
import time

import pytest

from pi.car_service.main import STOP, CarCore, Service
from shared import config
from shared import messages as m
from tests.fakes.fake_control import FakeControl
from tests.fakes.fake_line import CENTER, NOTHING, STOPLINE, FakeLine, route
from tests.fakes.fake_serial import FakeSerial

CAR = dict(config.load_car(require_tuned=False), speed_normal=100, speed_slow=50, turn_small=20, turn_big=60)
MP = config.load_map()
TIMING = config.load_network()["timing"]
CTL = m.Sender()
DT = 0.02   # 센서 50Hz


def make_core(car=CAR, mp=MP, log=None):
    return CarCore(car, mp, TIMING, m.Sender(clock=lambda: 1000.0), log or (lambda type_, **f: None))


def cmd(core, name, now=0.0, car=1, **fields):
    core.handle(CTL.make(m.car_topic(car, name), **fields), now)
    return core.take_outbox()


def names(msgs):
    return [x["topic"].split("/")[1] for x in msgs]


def drive(core, readings, t):
    """센서 값마다 tick. (끝난 시각, 바퀴 값들, 보낸 메시지)."""
    motors, out = [], []
    for r in readings:
        motors.append(core.tick(r, t))
        out += core.take_outbox()
        t += DT
    return t, motors, out


def at_wait(core):
    out = cmd(core, m.SET_STATION, station="wait_1")
    assert core.state == "arrived" and "arrived" in names(out)


# 대기 1 → 보관함 2번 칸: 출발한 정지선은 안 셈, 1번 정지선 지남, 2번 정지선 시작에서 멈춤
TO_STORAGE_2 = route((STOPLINE, 3), (CENTER, 5),      # 대기 정거장 선 위에서 출발
                     (STOPLINE, 3), (CENTER, 5),      # 1번 (storage_1) 지남 → 그 뒤 느린 속도
                     (STOPLINE, 2))                   # 2번 시작 2번 읽힘 → 도착


# --- 상태와 명령 (4.4) -----------------------------------------------------------

def test_starts_idle_and_status_is_valid():
    core = make_core()
    core.tick(None, 0.0)
    st = core.take_outbox()[0]
    assert st["state"] == "idle" and st["station"] is None and st["serial_ok"] is False


@pytest.mark.parametrize("name, fields", [(m.GOTO, {"station": "storage_1"}), (m.RESUME, {})])
def test_idle_rejects(name, fields):
    core = make_core()
    out = cmd(core, name, **fields)
    assert names(out) == ["reject"] and out[0]["state"] == "idle"


def test_hand_dropped_silently_unless_arrived():
    core = make_core()
    assert cmd(core, m.HAND, angles=[90] * 6, ms=33) == []
    assert core.hand is None
    at_wait(core)
    cmd(core, m.HAND, angles=[1] * 6, ms=33)
    cmd(core, m.HAND, angles=[2] * 6, ms=33)
    assert core.take_hand() == ([2] * 6, 33)          # 가장 최근 것만
    assert core.take_hand() is None


def test_station_of_other_loop_or_other_car_rejected():
    core = make_core()
    at_wait(core)
    assert names(cmd(core, m.GOTO, station="meter")) == ["reject"]
    assert names(cmd(core, m.GOTO, station="storage_1", car=2)) == ["reject"]


def test_goto_same_station_arrives_without_moving():
    core = make_core()
    at_wait(core)
    out = cmd(core, m.GOTO, station="wait_1")
    assert "arrived" in names(out) and core.state == "arrived"


def test_goto_counts_stoplines_slows_and_stops_at_start():
    core = make_core()
    at_wait(core)
    out = cmd(core, m.GOTO, station="storage_2")
    assert core.state == "driving" and out[-1]["moving_to"] == "storage_2"
    t, motors, out = drive(core, TO_STORAGE_2, 0.0)
    assert core.state == "arrived" and core.station == "storage_2"
    assert {"topic": "car1/arrived", "station": "storage_2"}.items() <= [x for x in out if "arrived" in x["topic"]][0].items()
    assert motors[0] == (100, 100)                     # 보통 속도
    assert motors[-3] == (50, 50)                      # 목표 앞 구간은 느린 속도
    assert motors[-1] == STOP
    assert len(TO_STORAGE_2) == len(motors)


def test_wraps_around_loop():
    core = make_core()
    cmd(core, m.SET_STATION, station="cock")          # 5번 → 대기 0번 (= 6): 정지선 1개
    cmd(core, m.GOTO, station="wait_1")
    drive(core, route((STOPLINE, 3), (CENTER, 5), (STOPLINE, 2)), 0.0)
    assert core.station == "wait_1" and core.state == "arrived"


def test_invert_sensors():
    core = make_core(car=dict(CAR, invert_sensors=True))
    at_wait(core)
    cmd(core, m.GOTO, station="storage_1")
    inverted = [tuple(1 - v for v in r) for r in route((STOPLINE, 3), (CENTER, 5), (STOPLINE, 2))]
    drive(core, inverted, 0.0)
    assert core.station == "storage_1"


def test_follow_turns_toward_line():
    core = make_core()
    at_wait(core)
    cmd(core, m.GOTO, station="storage_3")
    _, motors, _ = drive(core, route((STOPLINE, 3), (CENTER, 3), ((0, 1, 0, 0), 1), ((0, 0, 1, 0), 1),
                                     ((1, 0, 0, 0), 1), ((0, 0, 0, 1), 1)), 0.0)
    assert motors[-4:] == [(80, 100), (100, 80), (40, 100), (100, 40)]


# --- 정지, 다시 가기 (6.2) -------------------------------------------------------

def test_stop_at_station_then_resume_returns_to_arrived():
    core = make_core()
    at_wait(core)
    cmd(core, m.STOP, reason="space")
    assert core.state == "stopped"
    cmd(core, m.RESUME)
    assert core.state == "arrived"


def test_stop_between_stations_then_goto_keeps_counting():
    core = make_core()
    at_wait(core)
    cmd(core, m.GOTO, station="storage_2")
    t, _, _ = drive(core, route((STOPLINE, 3), (CENTER, 5), (STOPLINE, 3), (CENTER, 3)), 0.0)  # 1번 지남
    cmd(core, m.STOP, now=t)
    assert core.tick(CENTER, t) == STOP
    cmd(core, m.RESUME, now=t)
    assert core.state == "stopped"                      # 정거장 사이: 그대로
    cmd(core, m.GOTO, now=t, station="storage_2")
    drive(core, route((CENTER, 3), (STOPLINE, 2)), t)   # 다음 정지선이 2번
    assert core.state == "arrived" and core.station == "storage_2"


# --- 안전 멈춤 (SR-38) --------------------------------------------------------------

def test_heartbeat_lost_stops_and_reports_after_link_returns():
    core = make_core()
    at_wait(core)
    cmd(core, m.HEARTBEAT, now=0.0)
    cmd(core, m.GOTO, now=0.0, station="storage_1")
    assert core.tick(STOPLINE, 0.5) != STOP
    assert core.tick(CENTER, 1.01) == STOP              # 1초 넘게 heartbeat 없음
    assert core.state == "stopped"
    out = core.take_outbox()
    assert "error" not in names(out)                    # 끊긴 동안에는 못 보냄
    out = cmd(core, m.HEARTBEAT, now=2.0)
    assert out[0]["code"] == "link_lost"
    assert core.tick(CENTER, 2.02) == STOP              # 연결이 돌아와도 스스로 안 움직임


def test_line_lost_after_search_time():
    core = make_core()
    at_wait(core)
    cmd(core, m.GOTO, station="storage_1")
    t, motors, out = drive(core, route((STOPLINE, 3), (CENTER, 3), ((0, 1, 0, 0), 1), (NOTHING, 30)), 0.0)
    assert motors[7] == motors[6] == (30, 50)           # 처음엔 하던 대로 돌며 찾음 (목표 앞이라 느린 속도)
    assert core.state == "error" and motors[-1] == STOP
    assert [x["code"] for x in out if x["topic"].endswith("error")] == ["line_lost"]


def test_line_missed_when_segment_too_long():
    raw = copy.deepcopy(MP.raw)
    for s in raw["stations"]:
        s["segment_s"] = 1.0
    core = make_core(mp=config.MapConfig(raw))
    at_wait(core)
    cmd(core, m.GOTO, station="storage_2")
    t, _, out = drive(core, route((STOPLINE, 3), (CENTER, 74)), 0.0)    # 1.0 × 1.5 = 1.5초 안 넘음
    assert core.state == "driving"
    t, _, out = drive(core, route((CENTER, 5)), t)
    assert core.state == "error"
    assert [x["code"] for x in out if x["topic"].endswith("error")] == ["line_missed"]


def test_line_check_off_when_segment_zero():
    assert make_core().line_check_on is False


# --- 시리얼, 시각 ------------------------------------------------------------------

def test_serial_fail_after_three_and_recovers_flag():
    core = make_core()
    at_wait(core)
    for _ in range(2):
        core.serial_result(False)
    assert core.state == "arrived"
    core.serial_result(False)
    out = core.take_outbox()
    assert core.state == "error" and [x["code"] for x in out if x["topic"].endswith("error")] == ["serial_fail"]
    core.serial_result(False)                           # 더 세지 않음
    assert core.serial_fails == 3
    core.serial_result(True)
    assert core.serial_ok and core.state == "error"     # 상태는 관제 명령으로만 바뀜


def test_time_sync_shifts_message_time():
    core = make_core()
    core.handle(CTL.make(m.car_topic(1, m.TIME_SYNC), now=1500.0), 0.0)
    core.take_outbox()
    core.status_msg()
    assert core.take_outbox()[0]["t"] == 1500.0          # 가짜 파이 시각 1000 + 차이 500


# --- 실제 TCP/UDP로 한 바퀴 ---------------------------------------------------------

def test_service_end_to_end():
    line = FakeLine(route((CENTER, 1)))
    link = FakeSerial()
    core = make_core()
    svc = Service(core, line, link, cmd_port=0, hand_port=0, sensor_hz=200, host="127.0.0.1")
    tcp_port, udp_port = svc.ports
    svc.start()
    import threading
    threading.Thread(target=svc.run, daemon=True).start()
    ctl = FakeControl("127.0.0.1", tcp_port)
    try:
        assert ctl.wait_for(m.STATUS)["state"] == "idle"   # 접속하자마자 status
        ctl.start_heartbeat(0.05)
        ctl.send(m.SET_STATION, station="wait_1")
        ctl.wait_for(m.ARRIVED, station="wait_1")
        assert link.moves() == []                          # 켜질 때·위치 지정 때 서보를 안 움직임

        ctl.send_hand(udp_port, [10, 20, 30, 40, 50, 60], 33)
        end = time.monotonic() + 2
        while not link.moves() and time.monotonic() < end:
            time.sleep(0.01)
        assert link.moves()[-1] == b"M 10 20 30 40 50 60 33\n"

        ctl.send_raw(b'{"topic":"car1/goto","seq":99,"t":1.0}\n')     # station 없음
        assert ctl.wait_for(m.REJECT)["cmd"] == "car1/goto"

        line.readings = route((STOPLINE, 3), (CENTER, 5), (STOPLINE, 3))
        ctl.send(m.GOTO, station="storage_1")
        ctl.wait_for(m.ARRIVED, station="storage_1")
        assert line.drives[-1] == STOP

        ctl.stop_heartbeat()
        ctl.wait_for(m.STATUS, state="stopped")            # 1초 넘게 heartbeat 없음 → 멈춤
    finally:
        ctl.close()
        svc.close()
