"""관제 + 가짜 작업차 2대 + 가짜 맵 비전을 실제로 이어 보는 시험 (6단계 마무리).

조종 화면 대신 WebSocket으로 ui/cmd를 보내고, 관제가 보내는 ui/state·ui/alert를 본다.

    브라우저 자리(이 시험) ─ws─▶ 관제 ─TCP·UDP─▶ 가짜 작업차 1, 2 (진짜 작업차 서비스 + 가짜 선로)
                                  ▲
                                  └─UDP vision/state─ 가짜 맵 비전
"""

import copy
import json
import socket
import time

import pytest
from websockets.sync.client import connect

from laptop.control_service.logbook import MemoryLog
from laptop.control_service.main import ControlService
from laptop.control_service.tasks import TaskStore
from shared import config
from shared import messages as m
from tests.fakes.fake_car import FakeCar, map_config
from tests.fakes.fake_vision import FakeVision, handle_command

SEGMENT = 0.5           # 정지선 사이 0.5초 (빨리)
CALIB = config.load_aihand("aihand_A")


def free_port(kind=socket.SOCK_STREAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class World:
    def __init__(self, tmp_path) -> None:
        net = copy.deepcopy(config.load_network(config.ROOT / "tests" / "fakes" / "network_local.yaml"))
        car_mp = map_config(SEGMENT)
        self.cars = {}
        for n in m.CAR_IDS:
            car = FakeCar(n, car_mp, net["timing"], cmd_port=0, hand_port=0, segment_s=SEGMENT, sensor_hz=200)
            car.start()
            self.cars[n] = car
            net["cars"][str(n)].update(cmd_port=car.ports[0], hand_port=car.ports[1])
        lp = net["laptop"]
        lp["web_port"] = free_port()
        for key in ("hand_pose_port", "vision_state_port", "hand_calib_port"):
            lp[key] = free_port(socket.SOCK_DGRAM)
        self.net = net
        self.vision = FakeVision(("127.0.0.1", lp["vision_state_port"]))
        self.vision.start()
        self.log = MemoryLog()
        calibs = {1: copy.deepcopy(CALIB), 2: copy.deepcopy(CALIB)}
        self.svc = ControlService(net, config.load_map(), calibs, TaskStore(tmp_path / "tasks"), self.log)
        self.svc.start_thread()
        self.ws = None                               # 조종 화면 자리: fixture가 연결해 넣음
        self.ui_out = m.Sender()
        self.alerts = []

    def cmd(self, action, **fields) -> None:
        self.ws.send(m.encode(self.ui_out.make(m.UI_CMD, action=action, **fields)).decode("utf-8"))

    def state_until(self, cond, timeout=5.0):
        """cond(ui/state)가 참이 될 때까지. 오는 ui/alert는 모아 둠."""
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            try:
                msg = json.loads(self.ws.recv(timeout=max(0.05, end - time.monotonic())))
            except TimeoutError:
                break
            if msg["topic"] == m.UI_ALERT:
                self.alerts.append(msg)
            elif msg["topic"] == m.UI_STATE:
                m.validate(msg)
                last = msg
                if cond(msg):
                    return msg
        raise AssertionError(f"ui/state가 기대대로 안 됨. 마지막: {last and last['cars']}")

    def place_and_go(self, n, wait_station, target):
        car = lambda st: st["cars"][str(n)]                                        # noqa: E731
        self.state_until(lambda st: car(st)["link"] and "set_station" in car(st)["allowed"])
        self.cmd("set_station", car=n, station=wait_station)
        self.state_until(lambda st: car(st)["station"] == wait_station and car(st)["pi_state"] == "arrived")
        self.cmd("set_mode", car=n, mode="manual")
        self.state_until(lambda st: car(st)["mode"] == "manual" and "goto" in car(st)["allowed"])
        self.cmd("goto", car=n, station=target)

    def close(self) -> None:
        self.svc.stop()
        self.vision.close()
        for car in self.cars.values():
            car.close()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    try:
        with connect(f"ws://127.0.0.1:{w.net['laptop']['web_port']}/ws", open_timeout=3) as ws:
            w.ws = ws
            yield w
    finally:
        w.close()


def test_control_links_both_fake_cars_and_vision(world):
    st = world.state_until(lambda st: all(st["cars"][k]["link"] for k in ("1", "2")) and st["vision"]["service"])
    assert st["cars"]["1"]["pi_state"] == "idle" and st["cars"]["2"]["pi_state"] == "idle"


def test_car1_place_goto_arrive(world):
    world.place_and_go(1, "wait_1", "storage_2")
    world.state_until(lambda st: st["cars"]["1"]["pi_state"] == "driving" and st["cars"]["1"]["moving_to"] == "storage_2")
    st = world.state_until(lambda st: st["cars"]["1"]["pi_state"] == "arrived" and st["cars"]["1"]["station"] == "storage_2")
    assert st["cars"]["1"]["mode"] == "manual"
    assert world.cars[1].track.line_under() == 2


def test_both_cars_move_at_once(world):
    world.place_and_go(1, "wait_1", "bench_1")
    world.place_and_go(2, "wait_2", "meter")
    world.state_until(lambda st: st["cars"]["1"]["station"] == "bench_1" and st["cars"]["2"]["station"] == "meter"
                      and st["cars"]["1"]["pi_state"] == st["cars"]["2"]["pi_state"] == "arrived", timeout=8.0)


def test_vision_console_shows_in_ui_state(world):
    world.state_until(lambda st: st["vision"]["service"] and st["slots"]["2"] == "occupied")
    handle_command(world.vision, "2")             # 칸 2 비움
    handle_command(world.vision, "led")
    st = world.state_until(lambda st: st["slots"]["2"] == "empty" and st["cock_led"] is True)
    assert st["slots"]["1"] == st["slots"]["3"] == "occupied"
    handle_command(world.vision, "b")             # 작업대에 바이알
    world.state_until(lambda st: st["bench_vial"] == "occupied")
    handle_command(world.vision, "off")           # 맵 비전 꺼짐
    world.state_until(lambda st: not st["vision"]["service"])


def test_line_lost_reaches_control(world):
    world.place_and_go(1, "wait_1", "cock")
    world.state_until(lambda st: st["cars"]["1"]["pi_state"] == "driving")
    world.cars[1].track.lose_line()
    st = world.state_until(lambda st: st["cars"]["1"]["pi_state"] == "error")
    assert any(a["code"] == "line_lost" and a["car"] == 1 for a in world.alerts)
    assert st["cars"]["1"]["mode"] == "stopped"
