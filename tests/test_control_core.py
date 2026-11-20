"""
관제 서비스 판단 부분(core.py) 규칙 시험 — 기준: 시스템 설계 5.2, 6, 7, 8, 9.3, 9.5, 11, 12장.

네트워크 없이 Control에 메시지와 시각(now)을 직접 넣고, 보낼 것(outbox)을 꺼내 확인한다.
시험 기댓값은 core.py가 아니라 설계 문서에서 가져온다.
"""

from __future__ import annotations

import copy
import json

import pytest

from shared import config
from shared import messages as m
from laptop.control_service.core import Control
from laptop.control_service.logbook import MemoryLog
from laptop.control_service.tasks import STORAGE_ANY, TaskStore

NET = config.load_network()
CALIB = config.load_aihand("aihand_A")

MARK = {"o": "occupied", "e": "empty", "u": "unknown"}

# aihand_A 보정값 + margin 5° 기준 (설계 9.3)
OPEN = [5, 145, 145, 145, 145]          # 손가락 편 각도를 5° 안쪽으로 자른 값
FIST = [150, 37, 36, 35, 15]            # 쥔 각도를 5° 안쪽으로 자른 값
F1 = [60, 100, 100, 100, 100, 90]       # 재생용 프레임 (범위 안)
F2 = [100, 60, 60, 60, 60, 120]
F3 = [80, 80, 80, 80, 80, 100]


class H:
    """시험 도우미: Control 하나 + 가짜 시각 + (원하면) 가짜 파이 응답."""

    def __init__(self, tmp_path, calibs=None) -> None:
        self.mp = config.load_map()
        self.log = MemoryLog()
        self.store = TaskStore(tmp_path / "tasks")
        if calibs is None:
            calibs = {1: copy.deepcopy(CALIB), 2: copy.deepcopy(CALIB)}
        self.c = Control(NET, self.mp, calibs, self.store, self.log)
        self.now = 100.0
        self.ui = m.Sender()
        self.pi = {1: m.Sender(), 2: m.Sender()}
        self.svc = m.Sender()
        self.outs = []
        self.hand_times = []        # (보낸 시각, 작업차, 각도)
        self.vis = None            # 마지막 vision/state: run() 동안 계속 다시 보냄
        self.fake_pi = set()        # 여기 있는 작업차는 goto·set_station·stop에 파이처럼 답함
        self.pending = []           # (도착 시각, 작업차, 정거장)

    # -------------------------------------------------------------- 시각
    def _pump(self) -> None:
        while True:
            out = self.c.take_outbox()
            if not out:
                return
            for kind, n, msg in out:
                self.outs.append((kind, n, msg))
                if kind == "udp":
                    self.hand_times.append((self.now, n, msg["angles"]))
                if kind == "tcp" and n in self.fake_pi:
                    self._pi_answer(n, msg)

    def _pi_answer(self, n, msg) -> None:
        name = m.split_car_topic(msg["topic"])[1]
        car = self.c.cars[n]
        if name == m.GOTO:
            st = msg["station"]
            if car.pi_state == "arrived" and car.station == st:
                self.pending.append((self.now, n, st))
            else:
                self.status(n, "driving", None, st)
                self.pending.append((self.now + 0.5, n, st))
        elif name == m.SET_STATION:
            self.arrived(n, msg["station"])
        elif name == m.STOP:
            self.pending = [p for p in self.pending if p[1] != n]
            self.status(n, "stopped", None if car.between else car.station, None)

    def tick(self) -> None:
        self.c.tick(self.now)
        self._pump()

    def run(self, seconds: float, step: float = 0.05) -> None:
        for _ in range(int(round(seconds / step))):
            self.now = round(self.now + step, 6)
            if self.vis is not None:
                self._send_vision()
            due = [p for p in self.pending if p[0] <= self.now]
            self.pending = [p for p in self.pending if p[0] > self.now]
            for _, n, st in due:
                self.arrived(n, st)
            self.tick()

    # -------------------------------------------------------------- 넣기
    def _send_vision(self) -> None:
        slots, bench, led = self.vis
        msg = self.svc.make(m.VISION_STATE, slots={k: MARK[v] for k, v in zip(m.SLOT_KEYS, slots)},
                            bench=MARK[bench], cock_led=led, fps=30.0)
        self.c.on_vision(msg, self.now)

    def vision(self, slots="ooo", bench="e", led=False) -> None:
        self.vis = (slots, bench, led)
        self._send_vision()
        self.tick()

    def vision_off(self) -> None:
        self.vis = None

    def link(self, n, up=True) -> None:
        self.c.on_link(n, up, self.now)
        self._pump()

    def status(self, n, state, station=None, moving_to=None) -> None:
        msg = self.pi[n].make(m.car_topic(n, m.STATUS), state=state, station=station,
                              moving_to=moving_to, serial_ok=True)
        self.c.on_car_msg(n, msg, self.now)
        self._pump()

    def arrived(self, n, station) -> None:
        self.c.on_car_msg(n, self.pi[n].make(m.car_topic(n, m.ARRIVED), station=station), self.now)
        self._pump()
        self.status(n, "arrived", station, None)

    def error(self, n, code, text="시험") -> None:
        self.c.on_car_msg(n, self.pi[n].make(m.car_topic(n, m.ERROR), code=code, text=text), self.now)
        self._pump()

    def reject(self, n, cmd, state="arrived") -> None:
        msg = self.pi[n].make(m.car_topic(n, m.REJECT), cmd=m.car_topic(n, cmd), state=state, text="시험")
        self.c.on_car_msg(n, msg, self.now)
        self._pump()

    def cmd(self, action, **fields) -> None:
        self.c.on_ui_cmd(self.ui.make(m.UI_CMD, action=action, **fields), self.now)
        self._pump()

    def hand(self, fingers=(0, 0, 0, 0, 0), wrist=0.0, detected=True, calibrated=True) -> None:
        msg = self.svc.make(m.HAND_POSE, detected=detected,
                            fingers=list(fingers) if detected else None,
                            wrist=wrist if detected else None, calibrated=calibrated, fps=30.0)
        self.c.on_hand_pose(msg, self.now)
        self._pump()

    def ready(self, n, station=None) -> None:
        """연결 + 정거장에 서 있음."""
        self.link(n)
        self.status(n, "arrived", station or self.mp.wait_station(n), None)

    def save_task(self, car, steps, name="시험") -> str:
        return self.store.save(name, car, "aihand_A", steps)

    # -------------------------------------------------------------- 꺼내기
    def clear(self) -> None:
        self.outs = []

    def tcp(self, n=None, name=None):
        out = []
        for kind, car, msg in self.outs:
            if kind != "tcp" or (n is not None and car != n):
                continue
            if name is not None and m.split_car_topic(msg["topic"])[1] != name:
                continue
            out.append(msg)
        return out

    def hands(self, n=None):
        return [msg["angles"] for kind, car, msg in self.outs if kind == "udp" and (n is None or car == n)]

    def hand_msgs(self, n=None):
        return [msg for kind, car, msg in self.outs if kind == "udp" and (n is None or car == n)]

    def alerts(self, code=None):
        return [msg for kind, _, msg in self.outs if kind == "alert" and (code is None or msg["code"] == code)]

    def mode(self, n) -> str:
        return self.c.cars[n].mode

    def allowed(self, n):
        return self.c.allowed(self.c.cars[n])

    def manual(self, n=1, start=True) -> None:
        """시작 절차(파이 idle → [위치 지정] → 처음 자세: 손가락 폄) + 조종 모드 + 조종 대상 (+ 손을 펴서 시작)."""
        self.link(n)
        self.status(n, "idle")
        self.cmd("set_station", car=n, station=self.mp.wait_station(n))
        self.arrived(n, self.mp.wait_station(n))
        if self.c.cars[n].calib is not None:
            assert self.c.cars[n].last_angles is not None   # 관제가 AiHand 모양을 앎
        self.cmd("select_car", car=n)
        self.cmd("set_mode", car=n, mode="manual")
        assert self.mode(n) == "manual"
        if start:
            self.start_hand()

    def start_hand(self, wrist=0.0) -> None:
        """시작 신호: 손을 쫙 펴서 open_hold_s 동안 유지 (설계 9.6). 이 편 손은 AiHand에 보내지 않음."""
        hold = self.mp.hand_follow["open_hold_s"]
        self.hand((0, 0, 0, 0, 0), wrist)
        self.now = round(self.now + hold, 6)
        self.hand((0, 0, 0, 0, 0), wrist)
        assert not self.c.gate.locked


@pytest.fixture
def h(tmp_path):
    return H(tmp_path)


def _steps_storage_bench():
    """작업차 1 작업: 보관함에서 집기 → 작업대에 놓기 (마지막 대기 정거장 이동은 일부러 뺌)."""
    return [
        {"type": "goto", "station": STORAGE_ANY},
        {"type": "hand", "frames": [{"dt": 0.0, "angles": F1}, {"dt": 0.1, "angles": F2}]},
        {"type": "goto", "station": "bench_1"},
        {"type": "hand", "frames": [{"dt": 0.0, "angles": F3}]},
    ]


# ============================================================== 5.2 보류
class TestHold:
    def test_car1_at_storage_holds_only_that_slot(self, h):
        h.vision("ooo", "e")
        h.ready(1, "storage_2")
        h.tick()
        assert h.c.slots() == {"1": "occupied", "2": "unknown", "3": "occupied"}
        assert h.c.bench_view.value == "empty"

    def test_car1_driving_holds_all_slots_and_bench(self, h):
        h.vision("ooo", "e")
        h.ready(1)
        h.status(1, "driving", None, "storage_1")
        h.tick()
        assert set(h.c.slots().values()) == {"unknown"}
        assert h.c.bench_view.value == "unknown"

    def test_car1_at_wait_holds_nothing(self, h):
        h.vision("oeo", "e")
        h.ready(1, "wait_1")
        h.tick()
        assert h.c.slots() == {"1": "occupied", "2": "empty", "3": "occupied"}

    def test_car2_holds_bench_but_not_slots(self, h):
        h.vision("ooo", "e")
        h.ready(2, "bench_2")
        h.tick()
        assert h.c.bench_view.value == "unknown"
        assert set(h.c.slots().values()) == {"occupied"}
        h.status(2, "driving", None, "meter")
        h.tick()
        assert h.c.bench_view.value == "unknown"
        assert set(h.c.slots().values()) == {"occupied"}

    def test_car_stopped_between_stations_keeps_hold(self, h):
        h.vision("ooo", "e")
        h.ready(1)
        h.status(1, "driving", None, "storage_1")
        h.status(1, "stopped", None, None)           # 파이: 멈추면 moving_to가 null
        h.run(1.0)
        assert set(h.c.slots().values()) == {"unknown"}
        assert h.c.bench_view.value == "unknown"

    def test_unlinked_car_is_not_a_cause(self, h):
        h.vision("ooo", "e")
        h.ready(1)
        h.status(1, "driving", None, "storage_1")
        h.tick()
        assert h.c.slots()["1"] == "unknown"
        h.link(1, False)
        h.run(0.6)                                    # 보류가 풀린 뒤 0.5초 같은 값
        assert set(h.c.slots().values()) == {"occupied"}

    def test_value_must_stay_same_for_settle_time_after_hold(self, h):
        h.vision("ooo", "e")
        h.ready(1, "storage_1")
        h.tick()
        assert h.c.slots()["1"] == "unknown"
        h.status(1, "driving", None, "storage_2")
        h.arrived(1, "storage_2")                     # 1번 칸 보류가 풀림
        h.vision("eoo", "e")                          # 집어 갔으므로 1번 칸 비었음
        h.run(0.3)
        assert h.c.slots()["1"] == "unknown"          # 아직 0.5초 안 됨
        h.vision("ooo", "e")                          # 흔들림: 값이 바뀌면 다시 0.5초
        h.run(0.3)
        assert h.c.slots()["1"] == "unknown"
        h.run(0.3)
        assert h.c.slots()["1"] == "occupied"


# ============================================================== 6.3 작업대
class TestBench:
    def test_start_empty(self, h):
        h.vision("ooo", "e")
        assert h.c.bench == "empty" and h.c.bench_ready()
        assert not h.alerts("bench_not_empty")

    def test_fill_cycle(self, h):
        h.vision("ooo", "e")
        h.vision("ooo", "o", led=False)
        assert h.c.bench == "filling"
        h.vision("ooo", "o", led=True)
        assert h.c.bench == "filled"
        h.vision("ooo", "o", led=False)               # 콕을 닫아도 담기 끝 유지
        assert h.c.bench == "filled"
        h.vision("ooo", "e", led=False)
        assert h.c.bench == "empty"

    def test_filling_vial_removed_goes_empty(self, h):
        h.vision("ooo", "e")
        h.vision("ooo", "o")
        h.vision("ooo", "e")
        assert h.c.bench == "empty"

    def test_cock_already_open_when_vial_placed(self, h):
        h.vision("ooo", "e")
        h.manual(1)
        h.clear()
        h.vision("ooo", "o", led=True)
        assert h.c.bench == "empty"
        assert len(h.alerts("cock_open")) == 1
        assert h.mode(1) == "stopped"
        assert h.tcp(1, m.STOP)
        h.vision("ooo", "o", led=True)                # 같은 상황이 이어져도 경고는 한 번
        assert len(h.alerts("cock_open")) == 1

    def test_leftover_vial_at_start(self, h):
        h.vision("ooo", "o")
        assert h.c.bench == "empty"
        assert len(h.alerts("bench_not_empty")) == 1
        assert not h.c.bench_ready()
        h.vision("ooo", "o")
        assert h.c.bench == "empty"                   # 남은 바이알로 담는 중이 되지 않음
        h.vision("ooo", "e")
        assert h.c.bench_ready()
        h.vision("ooo", "o")
        assert h.c.bench == "filling"

    def test_led_during_hold_counts_as_filled(self, h):
        h.vision("ooo", "e")
        h.ready(2)
        h.status(2, "driving", None, "bench_2")
        h.tick()                                      # 작업대 보류 시작 (LED 꺼짐)
        h.vision("ooo", "o", led=True)              # 보류 중 콕 LED 켜졌다가
        h.vision("ooo", "o", led=False)               # 꺼짐
        assert h.c.bench == "empty"
        h.status(2, "driving", None, "meter")
        h.arrived(2, "meter")                         # 보류 풀림
        h.run(0.6)
        assert h.c.bench == "filled"
        assert not h.alerts("cock_open")

    def test_led_on_when_hold_started_is_cock_open(self, h):
        h.vision("ooo", "e", led=True)
        h.ready(2)
        h.status(2, "driving", None, "bench_2")
        h.vision("ooo", "o", led=True)
        h.arrived(2, "meter")
        h.run(0.6)
        assert h.c.bench == "empty"
        assert h.alerts("cock_open")

    def test_hold_without_led_is_filling(self, h):
        h.vision("ooo", "e")
        h.ready(2)
        h.status(2, "driving", None, "bench_2")
        h.vision("ooo", "o")
        h.arrived(2, "meter")
        h.run(0.6)
        assert h.c.bench == "filling"


# ============================================================== 6.1, 12 모드와 버튼 조건
class TestModes:
    def test_allowed_idle(self, h):
        h.ready(1)
        a = h.allowed(1)
        for k in ("select_car", "stop", "stop_all", "set_mode:manual", "set_mode:teach", "set_station", "start"):
            assert k in a
        for k in ("set_mode:idle", "record_end", "goto", "mark_result"):
            assert k not in a

    def test_allowed_manual(self, h):
        h.manual(1)
        a = h.allowed(1)
        for k in ("set_mode:idle", "set_mode:teach", "goto"):
            assert k in a
        for k in ("set_mode:manual", "set_station", "start", "record_end"):
            assert k not in a

    def test_allowed_teach(self, h):
        h.manual(1)
        h.cmd("set_mode", car=1, mode="teach")
        a = h.allowed(1)
        assert "record_end" in a and "goto" in a
        for k in ("set_mode:idle", "set_mode:manual", "set_mode:teach", "start", "set_station"):
            assert k not in a

    def test_allowed_stopped_only_manual(self, h):
        h.manual(1)
        h.cmd("stop", car=1)
        a = h.allowed(1)
        assert "set_mode:manual" in a and "set_station" in a
        for k in ("set_mode:idle", "set_mode:teach", "start", "goto", "record_end"):
            assert k not in a

    def test_start_only_at_wait_station(self, h):
        h.ready(1, "storage_1")
        assert "start" not in h.allowed(1)
        h.status(1, "arrived", "wait_1")
        assert "start" in h.allowed(1)
        h.status(1, "driving", None, "storage_1")
        assert "start" not in h.allowed(1)

    def test_goto_not_while_driving(self, h):
        h.manual(1)
        h.status(1, "driving", None, "storage_1")
        assert "goto" not in h.allowed(1)

    def test_rejected_cmd_keeps_mode(self, h):
        h.manual(1)
        h.cmd("stop", car=1)
        h.clear()
        h.cmd("set_mode", car=1, mode="teach")
        assert h.mode(1) == "stopped"
        assert h.alerts("cmd_rejected")

    def test_stop_only_that_car(self, h):
        h.manual(1)
        h.manual(2)
        h.clear()
        h.cmd("stop", car=1)
        assert h.mode(1) == "stopped" and h.mode(2) == "manual"
        assert h.tcp(1, m.STOP) and not h.tcp(2, m.STOP)

    def test_stop_all(self, h):
        h.manual(1)
        h.ready(2)
        h.clear()
        h.cmd("stop_all")
        assert h.mode(1) == "stopped" and h.mode(2) == "stopped"
        assert h.tcp(1, m.STOP) and h.tcp(2, m.STOP)

    def test_stopped_to_manual_sends_resume_when_pi_stopped(self, h):
        h.fake_pi.add(1)
        h.manual(1)
        h.cmd("stop", car=1)
        assert h.c.cars[1].pi_state == "stopped"
        h.clear()
        h.cmd("set_mode", car=1, mode="manual")
        assert h.mode(1) == "manual"
        assert h.tcp(1, m.RESUME)

    def test_no_resume_after_set_station(self, h):
        h.fake_pi.add(1)
        h.manual(1)
        h.cmd("stop", car=1)
        h.cmd("set_station", car=1, station="wait_1")
        assert h.c.cars[1].pi_state == "arrived"
        h.clear()
        h.cmd("set_mode", car=1, mode="manual")
        assert h.mode(1) == "manual"
        assert not h.tcp(1, m.RESUME)

    def test_set_station_sends_initial_pose(self, h):
        h.fake_pi.add(1)
        h.link(1)
        h.status(1, "idle", None, None)
        h.clear()
        h.cmd("set_station", car=1, station="wait_1")
        assert h.tcp(1, m.SET_STATION)[0]["station"] == "wait_1"
        assert h.mode(1) == "idle"                    # 위치 지정은 모드를 바꾸지 않음
        assert h.hands(1) == [OPEN + [CALIB["drive_wrist"]]]

    def test_set_station_other_loop_rejected(self, h):
        h.ready(1)
        h.clear()
        h.cmd("set_station", car=1, station="meter")
        assert h.alerts("cmd_rejected") and not h.tcp(1, m.SET_STATION)

    def test_ui_state_is_valid_and_set_mode_has_mode(self, h):
        h.manual(1)
        s = h.c.ui_state()                            # Sender.make가 docs/05 형식을 검사함
        assert s["topic"] == m.UI_STATE
        assert "set_mode:idle" in s["cars"]["1"]["allowed"]
        assert s["cars"]["2"]["link"] is False

    def test_ui_state_stations_per_car_in_line_order(self, h):
        mp = config.load_map()
        cars = h.c.ui_state()["cars"]
        for n in (1, 2):
            ids = [s["id"] for s in cars[str(n)]["stations"]]
            assert set(ids) == set(mp.stations_of_car(n))
            assert ids[0] == mp.wait_station(n)       # 대기 정거장(정지선 0)이 맨 앞
            assert [mp.station(i)["line"] for i in ids] == sorted(mp.station(i)["line"] for i in ids)
            assert all(s["name"] == mp.station(s["id"])["name"] for s in cars[str(n)]["stations"])


# ============================================================== 6.1, 9.3 손 명령
class TestHand:
    def test_hand_to_servo_angles(self, h):
        h.manual(1)
        h.clear()
        h.hand((0, 0, 0, 0, 0), 30.0)
        h.now += 0.05
        h.hand((1, 1, 1, 1, 1), 90.0)
        assert h.hands(1) == [OPEN + [120], FIST + [175]]

    def test_ms_is_interval(self, h):
        h.manual(1)
        h.clear()
        h.hand((0, 0, 0, 0, 0), 0.0)
        h.now += 0.05
        h.hand((0.5, 0, 0, 0, 0), 0.0)
        assert h.hand_msgs(1)[-1]["ms"] == 50

    def test_small_change_not_sent(self, h):
        h.manual(1)
        h.hand((0, 0, 0, 0, 0), 0.0)
        h.clear()
        h.hand((0, 0, 0, 0, 0), 1.0)                  # 1° 변화
        assert h.hands(1) == []
        h.hand((0, 0, 0, 0, 0), 2.0)
        assert h.hands(1) == [OPEN + [92]]

    @pytest.mark.parametrize("case", ["idle", "not_selected", "driving", "not_calibrated", "stopped"])
    def test_not_sent(self, h, case):
        if case == "idle":
            h.ready(1)
        else:
            h.manual(1)
        if case == "not_selected":
            h.cmd("select_car", car=2)
        elif case == "driving":
            h.status(1, "driving", None, "storage_1")
        elif case == "stopped":
            h.cmd("stop", car=1)
        h.clear()
        h.hand((0.5, 0.5, 0.5, 0.5, 0.5), 10.0, calibrated=(case != "not_calibrated"))
        assert h.hands() == []

    def test_no_aihand_calibration_blocks_hand(self, tmp_path):
        h = H(tmp_path, calibs={1: None, 2: copy.deepcopy(CALIB)})
        h.manual(1, start=False)
        # 보정 파일이 없으면 시작 신호도 보지 않음
        h.hand((0, 0, 0, 0, 0))
        h.now += 1.0
        h.hand((0, 0, 0, 0, 0))
        assert h.c.gate.locked and h.c.ui_state()["cars"]["1"]["hand_ctl"] == "no_calib"
        h.clear()
        h.hand((0.5, 0.5, 0.5, 0.5, 0.5), 10.0)
        assert h.hands() == []
        assert h.c.calib_missing() == [1]

    def test_hand_lost_alert(self, h):
        h.manual(1)
        h.hand()
        h.clear()
        h.hand(detected=False)
        assert h.alerts("hand_lost")
        assert h.mode(1) == "manual"

    def test_hand_service_down(self, h):
        h.manual(1)
        h.hand()
        h.clear()
        h.run(1.2)
        assert h.alerts("hand_service_down")
        assert h.mode(1) == "manual"
        assert "hand_calib" not in h.allowed(1)

    def test_hand_calib_needs_visible_hand(self, h):
        h.cmd("hand_calib", step="open")
        assert h.alerts("cmd_rejected")
        h.hand()
        h.clear()
        h.cmd("hand_calib", step="open")
        assert [msg["step"] for kind, _, msg in h.outs if kind == "calib"] == ["open"]


# ============================================================== 9.6 손 따라 하기 시작·이어 붙이기
HALF = [0.5] * 5


class TestHandStart:
    def test_locked_until_open_hand_held(self, h):
        h.manual(1, start=False)
        h.clear()
        h.hand((0.5, 0.5, 0.5, 0.5, 0.5), 10.0)       # 잠김: 아무 손 모양도 안 보냄
        h.hand((0, 0, 0, 0, 0))
        h.now += 0.2
        h.hand((0, 0, 0, 0, 0))                       # 0.3초가 안 됨
        assert h.c.gate.locked
        h.now += 0.1
        h.hand((0, 0, 0, 0, 0))                       # 0.3초 유지 → 시작
        assert not h.c.gate.locked
        assert h.hands() == []                        # 시작 신호의 편 손은 보내지 않음
        assert h.log.of("hand_start")
        h.hand((0, 0, 0, 0, 0), 20.0)
        assert h.hands(1) == [OPEN + [110]]           # 그 뒤 동작부터 따라 함

    def test_open_must_be_continuous(self, h):
        h.manual(1, start=False)
        h.hand((0, 0, 0, 0, 0))
        h.now += 0.2
        h.hand((0, 0.5, 0, 0, 0))                     # 중간에 손가락 하나를 굽힘 → 처음부터
        h.now += 0.2
        h.hand((0, 0, 0, 0, 0))
        assert h.c.gate.locked
        h.now += 0.3
        h.hand((0, 0, 0, 0, 0))
        assert not h.c.gate.locked

    def _holding(self, h):
        """AiHand가 주먹(바이알을 쥔 것)인 채로 잠김 → 손을 펴서 시작."""
        h.manual(1)
        h.hand((1, 1, 1, 1, 1), 0.0)
        assert h.c.cars[1].last_angles[:5] == FIST
        h.cmd("set_mode", car=1, mode="teach")        # 모드가 바뀌면 잠김
        assert h.c.gate.locked
        h.start_hand()
        h.clear()

    def test_holding_hand_does_not_open_after_start(self, h):
        self._holding(h)
        assert h.c.ui_state()["cars"]["1"]["hand_ctl"] == "catching"
        h.hand((0.5, 0.5, 0.5, 0.5, 0.5), 30.0)       # 쥐러 가는 중: 손가락은 그대로, 손목만
        assert h.hands(1) == [FIST + [120]]
        h.hand((1, 1, 0.5, 0.5, 0.5), 30.0)           # 엄지·검지만 AiHand에 닿음
        assert h.c.gate.attached == [True, True, False, False, False]
        h.hand((0.8, 0.8, 0.5, 0.5, 0.5), 30.0)       # 붙은 손가락은 이제 따라 함, 나머지는 그대로
        last = h.hands(1)[-1]
        assert last[2:5] == FIST[2:5] and last[0] != FIST[0] and last[1] != FIST[1]
        h.hand((1, 1, 1, 1, 1), 30.0)
        assert h.c.gate.all_attached()
        assert h.c.ui_state()["cars"]["1"]["hand_ctl"] == "following"
        h.hand(HALF, 30.0)                            # 모두 붙은 뒤에는 펴는 것도 따라 함
        assert h.hands(1)[-1][:5] == pose_fingers(HALF)

    def test_unknown_aihand_is_treated_as_holding(self, h):
        """관제를 다시 켜서 AiHand 모양을 모름 (설계 9.6): 주먹을 쥐어 5개 모두 붙기 전에는 아무것도 안 보냄."""
        h.ready(1)
        h.cmd("select_car", car=1)
        h.cmd("set_mode", car=1, mode="manual")
        assert h.c.cars[1].last_angles is None
        h.start_hand()
        h.clear()
        h.hand((0, 0, 0, 0, 0), 30.0)                 # 편 손: 붙지 않음, 손목도 안 보냄
        h.hand((1, 1, 1, 1, 0.5), 30.0)               # 4개만 붙음: 아직 안 보냄
        assert h.hands() == []
        assert h.c.ui_state()["cars"]["1"]["aihand_fingers"] is None
        assert h.c.ui_state()["cars"]["1"]["hand_ctl"] == "catching"
        h.hand((1, 1, 1, 1, 1), 30.0)
        assert h.hands(1) == [FIST + [120]]
        h.hand((0, 0, 0, 0, 0), 30.0)                 # 그 뒤로는 펴는 것도 따라 함
        assert h.hands(1)[-1] == OPEN + [120]

    def test_catch_tolerance(self, h):
        self._holding(h)
        tol = h.mp.hand_follow["catch_tol"]
        h.hand([1 - tol - 0.05] * 5, 0.0)
        assert not any(h.c.gate.attached)
        h.hand([1 - tol + 0.01] * 5, 0.0)
        assert h.c.gate.all_attached()

    def test_short_loss_continues_long_loss_locks(self, h):
        h.manual(1)
        lock_s = h.mp.hand_follow["lost_lock_s"]
        h.hand(detected=False)
        h.now += lock_s - 0.1
        h.hand()                                      # 잠깐 놓침: 이어서 따라 함 (SR-09)
        assert not h.c.gate.locked
        h.hand(detected=False)
        h.now += lock_s + 0.1
        h.hand(detected=False)
        assert not h.c.gate.locked                    # 안 보이는 동안에는 그대로
        h.hand()
        assert h.c.gate.locked                        # 길게 놓친 뒤 다시 보임 → 잠김

    @pytest.mark.parametrize("how", ["mode", "select_car", "arrived", "hand_calib", "hand_service_down"])
    def test_lock_triggers(self, h, how):
        h.fake_pi.add(1)
        h.ready(2)
        h.manual(1)
        if how == "mode":
            h.cmd("set_mode", car=1, mode="teach")
        elif how == "select_car":
            h.cmd("select_car", car=2)
            assert h.c.gate.locked
            h.manual(1)                                # 되돌아와도 다시 펴야 함
            h.cmd("select_car", car=2)
            h.cmd("select_car", car=1)
        elif how == "arrived":
            h.cmd("goto", car=1, station="storage_1")
            h.run(1.0)
            assert h.c.cars[1].station == "storage_1"
        elif how == "hand_calib":
            h.hand()
            h.cmd("hand_calib", step="open")
        elif how == "hand_service_down":
            h.run(1.2)
        assert h.c.gate.locked
        assert h.log.of("hand_lock")

    def test_other_car_mode_change_does_not_lock(self, h):
        h.ready(2)
        h.manual(1)
        h.cmd("set_mode", car=2, mode="manual")
        assert not h.c.gate.locked

    def test_ui_state_hand_fields(self, h):
        h.ready(2)
        h.manual(1, start=False)
        h.hand(HALF)
        st = h.c.ui_state()
        assert st["cars"]["1"]["hand_ctl"] == "locked"
        assert st["cars"]["2"]["hand_ctl"] == "not_selected"
        assert st["cars"]["2"]["hand_attached"] == [False] * 5
        h.start_hand()
        h.hand((1, 1, 1, 1, 1), 0.0)
        st = h.c.ui_state()
        assert st["cars"]["1"]["hand_ctl"] == "following"
        assert st["cars"]["1"]["aihand_fingers"] == [pytest.approx(r, abs=0.05) for r in (0.97, 0.96, 0.96, 0.96, 0.96)]
        assert st["hand"]["fingers"] == [1, 1, 1, 1, 1]
        h.hand(detected=False)
        st = h.c.ui_state()
        assert st["hand"]["fingers"] is None and st["cars"]["1"]["hand_ctl"] == "hand_lost"
        h.hand()
        h.status(1, "driving", None, "storage_1")
        assert h.c.ui_state()["cars"]["1"]["hand_ctl"] == "moving"

    @pytest.mark.parametrize("why", ["mode", "link", "hand_service", "hand_uncalibrated"])
    def test_ui_state_reasons(self, h, why):
        if why == "mode":
            h.ready(1)
            h.hand()
        elif why == "link":
            h.manual(1)
            h.link(1, False)
            h.cmd("set_mode", car=1, mode="manual")
        elif why == "hand_service":
            h.ready(1)
            h.cmd("set_mode", car=1, mode="manual")
        else:
            h.ready(1)
            h.cmd("set_mode", car=1, mode="manual")
            h.hand(calibrated=False)
        assert h.c.ui_state()["cars"]["1"]["hand_ctl"] == why


def pose_fingers(r):
    from laptop.control_service import pose
    return pose.hand_to_servos(r, 0.0, CALIB)[:5]


def test_fingers_r_round_trip():
    from laptop.control_service.handgate import fingers_r
    assert fingers_r(None, CALIB) is None
    assert fingers_r(OPEN + [90], CALIB) == [pytest.approx(x, abs=0.05) for x in (0.03, 0.04, 0.04, 0.04, 0.04)]
    raw = [s["closed"] for s in CALIB["servos"][:5]] + [90]
    assert fingers_r(raw, CALIB) == [1.0] * 5


# ============================================================== 9.5 주행 자세
class TestDrivePose:
    def test_wrist_turns_before_goto(self, h):
        h.manual(1)
        h.hand((1, 1, 1, 1, 1), 30.0)                 # 바이알을 쥔 손
        h.clear()
        h.cmd("goto", car=1, station="storage_1")
        assert h.hands(1) == [FIST + [CALIB["drive_wrist"]]]   # 손목만 바뀜
        assert not h.tcp(1, m.GOTO)
        h.hand((0, 0, 0, 0, 0), 0.0)                  # 기다리는 동안 손 명령은 안 보냄
        assert len(h.hands(1)) == 1
        h.run(0.3)
        assert h.tcp(1, m.GOTO)[0]["station"] == "storage_1"

    def test_drive_pose_disabled(self, h):
        h.mp.drive_pose["enabled"] = False
        h.manual(1)
        h.clear()
        h.cmd("goto", car=1, station="storage_1")
        assert h.hands(1) == []
        assert h.tcp(1, m.GOTO)

    def test_goto_other_loop_rejected(self, h):
        h.manual(1)
        h.clear()
        h.cmd("goto", car=1, station="meter")
        assert h.alerts("cmd_rejected")
        assert not h.tcp(1, m.GOTO) and h.hands(1) == []


# ============================================================== 8 가르치기
class TestTeach:
    def test_record_and_save(self, h):
        h.fake_pi.add(1)
        h.manual(1)
        h.cmd("set_mode", car=1, mode="teach")
        h.cmd("goto", car=1, station="storage_2")
        h.run(1.0)
        assert h.c.cars[1].station == "storage_2"
        h.start_hand()                                # 도착하면 잠김 → 손을 펴서 시작 (기록 안 됨)
        h.hand((0, 0, 0, 0, 0), -30.0)                # 주행 자세(손목 90)와 달라야 보냄
        h.now += 0.1
        h.hand((1, 1, 1, 1, 1), -30.0)
        h.cmd("goto", car=1, station="bench_1")
        h.run(1.0)
        h.start_hand()                                # AiHand는 쥔 채: 편 손은 안 보냄
        h.hand((1, 1, 1, 1, 1), -30.0)                # 주먹을 쥐면 손가락이 붙고 손목이 따라 감
        assert h.c.ui_state()["cars"]["1"]["recording_steps"] == 4
        h.cmd("record_end", car=1, name="분주 1회")
        assert h.mode(1) == "manual"
        tasks = h.store.list()
        assert len(tasks) == 1 and tasks[0]["name"] == "분주 1회" and tasks[0]["car"] == 1
        steps = h.store.load(tasks[0]["id"])["steps"]
        assert [s["type"] for s in steps] == ["goto", "hand", "goto", "hand"]
        assert steps[0]["station"] == STORAGE_ANY      # 보관함은 storage_*로 저장
        assert steps[2]["station"] == "bench_1"
        assert [f["angles"] for f in steps[1]["frames"]] == [OPEN + [60], FIST + [60]]   # 주행 자세는 기록 안 함
        assert steps[1]["frames"][0]["dt"] == 0.0
        assert steps[1]["frames"][1]["dt"] == pytest.approx(0.1)

    def test_record_end_without_steps(self, h):
        h.manual(1)
        h.cmd("set_mode", car=1, mode="teach")
        h.clear()
        h.cmd("record_end", car=1, name="빈 작업")
        assert h.alerts("cmd_rejected")
        assert h.mode(1) == "teach" and h.store.list() == []

    def test_stop_discards_recording(self, h):
        h.manual(1)
        h.cmd("set_mode", car=1, mode="teach")
        h.hand()
        h.cmd("stop", car=1)
        assert h.c.cars[1].recorder is None
        assert h.log.of("record_discard")


# ============================================================== 7, 8 혼자 하기
class TestAuto:
    def _start(self, h, steps, car=1, repeat=False, slots="ooo"):
        h.fake_pi.add(car)
        h.vision(slots, "e")
        h.ready(car)
        task = h.save_task(car, steps)
        h.cmd("start", car=car, task=task, repeat=repeat)
        return task

    def test_one_cycle(self, h):
        self._start(h, _steps_storage_bench(), slots="eoo")
        assert h.mode(1) == "auto"
        h.run(6.0)
        gotos = [msg["station"] for msg in h.tcp(1, m.GOTO)]
        assert gotos == ["storage_2", "bench_1", "wait_1"]          # 가장 작은 칸 + 대기 정거장 자동 복귀
        dw = CALIB["drive_wrist"]
        first_drive = OPEN + [dw]                                    # 아직 보낸 손이 없음 → 처음 자세
        assert h.hands(1) == [first_drive, F1, F2, F2, F2,           # 마지막 프레임 3번
                              F2[:5] + [dw], F3, F3, F3, F3[:5] + [dw]]
        assert h.mode(1) == "idle"
        assert h.alerts("task_done")
        st = h.c.ui_state()["cars"]["1"]
        assert st["result_pending"] and st["result_cycle"] == 1 and "mark_result" in st["allowed"]
        h.cmd("mark_result", car=1, cycle=1, result="success")
        assert h.log.of("result_mark")[0]["result"] == "success"
        assert "mark_result" not in h.allowed(1)

    def test_replay_frame_timing(self, h):
        self._start(h, _steps_storage_bench())
        h.run(1.5)                                    # 주행 자세 0.3초 → goto → 도착(0.5초) → 프레임
        sent = [(t, a) for t, n, a in h.hand_times if n == 1]
        i = next(k for k, (_, a) in enumerate(sent) if a == F1)
        times = [t for t, _ in sent[i:i + 4]]
        assert [a for _, a in sent[i:i + 4]] == [F1, F2, F2, F2]
        gaps = [round(b - a, 3) for a, b in zip(times, times[1:])]
        assert gaps == [0.1, 0.1, 0.1]                # dt 간격, 마지막 프레임은 0.1초 간격으로 3번
        f1_msg = next(msg for msg in h.hand_msgs(1) if msg["angles"] == F1)
        assert f1_msg["ms"] == 100                    # 다음 프레임까지 dt

    def test_vial_not_found(self, h):
        self._start(h, _steps_storage_bench(), slots="eee")
        h.run(0.5)
        assert h.alerts("vial_not_found")
        assert h.mode(1) == "idle" and not h.tcp(1, m.GOTO)

    def test_repeat_until_storage_empty(self, h):
        self._start(h, _steps_storage_bench(), repeat=True, slots="oee")
        h.run(1.0)                                    # storage_1 도착
        h.vision("eee", "e")                          # 집어 갔음 (작업대는 이 시험에서 보지 않음)
        h.run(6.0)
        assert [msg["station"] for msg in h.tcp(1, m.GOTO)] == ["storage_1", "bench_1", "wait_1"]
        assert h.mode(1) == "idle"
        assert h.alerts("storage_empty")
        assert not h.alerts("vial_not_found")
        assert h.c.cars[1].result_cycle == 1

    def test_car1_waits_for_empty_bench(self, h):
        h.fake_pi.add(1)
        h.vision("ooo", "e")
        h.vision("ooo", "o")                          # 작업대: 담는 중
        h.ready(1)
        task = h.save_task(1, _steps_storage_bench())
        h.cmd("start", car=1, task=task, repeat=False)
        h.run(1.0)
        assert h.mode(1) == "auto" and not h.tcp(1, m.GOTO) and h.hands(1) == []
        assert h.c.ui_state()["cars"]["1"]["step"] == "출발 조건 기다림"
        h.vision("ooo", "e")
        h.run(0.6)
        assert h.tcp(1, m.GOTO)

    def test_car2_waits_for_filled(self, h):
        steps = [{"type": "goto", "station": "bench_2"}, {"type": "hand", "frames": [{"dt": 0.0, "angles": F1}]}]
        self._start(h, steps, car=2)
        h.run(1.0)
        assert not h.tcp(2, m.GOTO) and h.hands(2) == []
        h.vision("ooo", "o")
        h.vision("ooo", "o", led=True)
        assert h.c.bench == "filled"
        h.run(0.6)
        assert h.tcp(2, m.GOTO)[0]["station"] == "bench_2"

    def test_car2_ends_when_car1_finished_and_bench_empty(self, h):
        h.fake_pi.update({1, 2})
        h.vision("ooo", "e")
        h.ready(1)
        h.ready(2)
        t2 = h.save_task(2, [{"type": "goto", "station": "bench_2"}], name="측정")
        t1 = h.save_task(1, [{"type": "hand", "frames": [{"dt": 0.0, "angles": F1}]}], name="손만")
        h.cmd("start", car=2, task=t2, repeat=True)
        h.run(0.5)
        assert h.mode(2) == "auto"
        h.cmd("start", car=1, task=t1, repeat=False)
        h.run(2.0)
        assert h.mode(1) == "idle"
        assert h.mode(2) == "idle"
        assert [a["car"] for a in h.alerts("storage_empty")] == [2]

    def test_car2_ends_after_car1_storage_empty(self, h):
        """작업차 1이 '보관함 비었음'으로 끝난 경우. 시각은 모두 넣어 준 now 기준이어야 함."""
        h.now = 1e9
        h.fake_pi.update({1, 2})
        h.vision("oee", "e")
        h.ready(1)
        h.ready(2)
        t2 = h.save_task(2, [{"type": "goto", "station": "bench_2"}], name="측정")
        t1 = h.save_task(1, [{"type": "goto", "station": STORAGE_ANY},
                             {"type": "hand", "frames": [{"dt": 0.0, "angles": F1}]}], name="집기")
        h.cmd("start", car=2, task=t2, repeat=True)
        h.cmd("start", car=1, task=t1, repeat=True)
        h.run(1.0)                                    # storage_1 도착
        h.vision("eee", "e")
        h.run(5.0)
        assert h.mode(1) == "idle" and h.mode(2) == "idle"
        assert sorted(a["car"] for a in h.alerts("storage_empty")) == [1, 2]

    def test_start_rejected_for_other_cars_task(self, h):
        h.vision("ooo", "e")
        h.ready(2)
        task = h.save_task(1, _steps_storage_bench())
        h.clear()
        h.cmd("start", car=2, task=task, repeat=False)
        assert h.mode(2) == "idle" and h.alerts("cmd_rejected")

    def test_start_rejected_without_vision(self, h):
        h.ready(1)
        task = h.save_task(1, _steps_storage_bench())
        h.cmd("start", car=1, task=task, repeat=False)
        assert h.mode(1) == "idle" and h.alerts("cmd_rejected")

    def test_cannot_delete_running_task(self, h):
        task = self._start(h, _steps_storage_bench())
        h.clear()
        h.cmd("delete_task", task=task)
        assert h.alerts("cmd_rejected") and len(h.store.list()) == 1

    def test_goto_timeout(self, h):
        h.manual(1)
        h.cmd("goto", car=1, station="storage_1")
        h.run(0.4)
        h.status(1, "driving", None, "storage_1")
        h.clear()
        h.run(30.0)
        assert h.alerts("goto_timeout")
        assert h.mode(1) == "stopped" and h.tcp(1, m.STOP)

    def test_vision_down_stops_only_auto_car(self, h):
        self._start(h, _steps_storage_bench())
        h.manual(2)
        h.vision_off()
        h.clear()
        h.run(1.2)
        assert h.alerts("vision_service_down")
        assert h.mode(1) == "stopped" and h.tcp(1, m.STOP)
        assert h.mode(2) == "manual"

    def test_pi_reject_goto_during_auto_stops(self, h):
        self._start(h, _steps_storage_bench())
        h.fake_pi.discard(1)
        h.run(0.4)
        h.clear()
        h.reject(1, m.GOTO)
        assert h.mode(1) == "stopped" and h.alerts("cmd_rejected")


# ============================================================== 11 오류
class TestErrors:
    @pytest.mark.parametrize("code,text", [("line_lost", "선 놓침"), ("line_missed", "정지선 놓침"),
                                           ("serial_fail", "AiHand 연결 오류")])
    def test_pi_error_stops_only_that_car(self, h, code, text):
        h.manual(1)
        h.manual(2)
        h.clear()
        h.error(1, code)
        assert h.mode(1) == "stopped" and h.mode(2) == "manual"
        a = h.alerts(code)
        assert len(a) == 1 and text in a[0]["text"] and a[0]["level"] == "error"
        assert not h.tcp(1, m.STOP)                   # 파이가 이미 멈춤

    def test_line_lost_recovery(self, h):
        h.fake_pi.add(1)
        h.manual(1)
        h.error(1, "line_lost")
        assert "set_station" in h.allowed(1)
        h.clear()
        h.cmd("set_station", car=1, station="storage_1")
        assert h.c.cars[1].pi_state == "arrived"
        h.cmd("set_mode", car=1, mode="manual")
        assert h.mode(1) == "manual" and not h.tcp(1, m.RESUME)

    @pytest.mark.parametrize("code", ["line_lost", "line_missed", "serial_fail"])
    def test_recovery_set_station_keeps_hand(self, h, code):
        """오류 복구 때 [위치 지정]은 처음 자세(손가락 폄)를 보내지 않음 — 쥔 바이알을 놓치지 않게 (설계 9.5)."""
        h.fake_pi.add(1)
        h.manual(1)
        h.hand((1, 1, 1, 1, 1), 0.0)                  # 바이알을 쥔 손
        h.error(1, code)
        h.clear()
        h.cmd("set_station", car=1, station="bench_1")
        assert h.c.cars[1].pi_state == "arrived"
        assert h.hands(1) == []

    def test_pi_link_lost_report_is_only_logged(self, h):
        h.manual(1)
        h.clear()
        h.error(1, "link_lost")
        assert h.mode(1) == "manual" and not h.alerts()
        assert h.log.of("report")

    def test_car_link_lost(self, h):
        h.manual(1)
        h.clear()
        h.link(1, False)
        assert h.alerts("car_link_lost")
        assert h.mode(1) == "stopped" and not h.tcp(1)
        assert not h.c.cars[1].link

    def test_car_link_lost_in_idle_then_recover(self, h):
        """대기 모드여도 정지 → 다시 연결 → [조종]이 resume을 보내 손 조종이 됨."""
        h.ready(1)
        h.cmd("select_car", car=1)
        h.link(1, False)
        assert h.mode(1) == "stopped"
        h.link(1, True)
        h.status(1, "stopped", "wait_1", None)        # 파이는 끊긴 동안 스스로 stopped (docs/05 4.4)
        h.clear()
        h.cmd("set_mode", car=1, mode="manual")
        assert h.tcp(1, m.RESUME)
        h.status(1, "arrived", "wait_1", None)        # 파이: resume → arrived
        h.start_hand()
        h.hand((0.5, 0.5, 0.5, 0.5, 0.5), 10.0)       # AiHand 모양을 모름 → 쥐고 있다고 봄 → 안 보냄
        assert not h.hands(1)
        h.hand((1, 1, 1, 1, 1), 10.0)                 # 주먹을 쥐면 이어짐
        assert h.hands(1)

    def test_link_up_sends_time_sync_first(self, h):
        h.link(1)
        msgs = h.tcp(1)
        assert m.split_car_topic(msgs[0]["topic"])[1] == m.TIME_SYNC

    def test_errors_are_logged(self, h):
        h.manual(1)
        h.error(1, "serial_fail")
        assert any(e["code"] == "serial_fail" for e in h.log.of("alert"))
        assert any(e["to"] == "stopped" for e in h.log.of("mode_change"))
