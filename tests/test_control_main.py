"""
관제 서비스 main.py 연결 시험 — 진짜 장치 없이 이 컴퓨터(127.0.0.1) 안에서.

가짜 파이(TCP 서버 + UDP 받기)와 WebSocket 클라이언트(브라우저 대신)를 띄워
접속·재접속·heartbeat·time_sync, 손 각도 전달, ui/state·ui/cmd, 틀린 메시지 처리를 본다.
기준: 시스템 설계 11장(연결 끊김), 인터페이스 정의 2~6장.
"""

from __future__ import annotations

import copy
import json
import socket
import threading
import time
from typing import Any, Dict, List

import pytest
from websockets.sync.client import connect

from shared import config
from shared import messages as m
from laptop.control_service.logbook import MemoryLog
from laptop.control_service.main import ControlService
from laptop.control_service.tasks import TaskStore

CALIB = config.load_aihand("aihand_A")


def free_port(kind=socket.SOCK_STREAM) -> int:
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, timeout=3.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class FakePi:
    """TCP 5006 자리를 열고 관제의 접속을 받는다. 받은 메시지를 기록하고, 보고를 보낼 수 있다."""

    def __init__(self, car: int) -> None:
        self.car = car
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        self.hand_port = self.udp.getsockname()[1]
        self.got: List[Dict[str, Any]] = []         # (TCP) 받은 메시지
        self.hands: List[Dict[str, Any]] = []       # (UDP) 받은 손 각도
        self.connects = 0
        self.conn_start: List[int] = []    # 접속마다 그때까지 받은 개수
        self.conn = None
        self.out = m.Sender()
        self._run = True
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._udp_loop, daemon=True).start()

    def _accept(self) -> None:
        self.srv.settimeout(0.2)
        while self._run:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            self.conn_start.append(len(self.got))
            self.connects += 1
            self.conn = conn
            reader = m.LineReader()
            conn.settimeout(0.2)
            while self._run:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                for line in reader.feed(data):
                    self.got.append(json.loads(line))

    def _udp_loop(self) -> None:
        while self._run:
            try:
                data, _ = self.udp.recvfrom(65536)
            except OSError:
                continue
            self.hands.append(json.loads(data))

    def send(self, name: str, **fields) -> None:
        self.conn.sendall(m.encode(self.out.make(m.car_topic(self.car, name), **fields)))

    def send_raw(self, data: bytes) -> None:
        self.conn.sendall(data)

    def drop(self) -> None:
        """연결만 끊음 (서버는 계속 열려 있어 관제가 다시 접속할 수 있음)."""
        self.conn.shutdown(socket.SHUT_RDWR)
        self.conn.close()

    def names(self) -> List[str]:
        return [m.split_car_topic(x["topic"])[1] for x in self.got]

    def close(self) -> None:
        self._run = False
        for s in (self.conn, self.srv, self.udp):
            try:
                s.close()
            except Exception:
                pass


class Rig:
    def __init__(self, tmp_path) -> None:
        self.pi = FakePi(1)
        net = copy.deepcopy(config.load_network())
        net["cars"]["1"].update(ip="127.0.0.1", cmd_port=self.pi.port, hand_port=self.pi.hand_port)
        net["cars"]["2"].update(ip="127.0.0.1", cmd_port=free_port())   # 아무도 안 받음 → 계속 재접속
        lp = net["laptop"]
        lp["web_port"] = free_port()
        lp["hand_pose_port"] = free_port(socket.SOCK_DGRAM)
        lp["vision_state_port"] = free_port(socket.SOCK_DGRAM)
        lp["hand_calib_port"] = free_port(socket.SOCK_DGRAM)
        self.net = net
        self.log = MemoryLog()
        calibs = {1: copy.deepcopy(CALIB), 2: copy.deepcopy(CALIB)}
        self.svc = ControlService(net, config.load_map(), calibs, TaskStore(tmp_path / "tasks"), self.log)
        self.svc.start_thread()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.svc_out = m.Sender()
        self.ui_out = m.Sender()

    def ws(self):
        return connect(f"ws://127.0.0.1:{self.net['laptop']['web_port']}/ws", open_timeout=3)

    def cmd(self, ws, action, **fields) -> None:
        ws.send(m.encode(self.ui_out.make(m.UI_CMD, action=action, **fields)).decode("utf-8"))

    def send_hand_pose(self, fingers, wrist) -> None:
        msg = self.svc_out.make(m.HAND_POSE, detected=True, fingers=list(fingers), wrist=wrist,
                                calibrated=True, fps=30.0)
        self.udp.sendto(m.encode(msg), ("127.0.0.1", self.net["laptop"]["hand_pose_port"]))

    def close(self) -> None:
        self.svc.stop()
        self.pi.close()
        self.udp.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def recv_until(ws, cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            msg = json.loads(ws.recv(timeout=max(0.05, end - time.monotonic())))
        except TimeoutError:
            break
        if cond(msg):
            return msg
    return None


def test_time_sync_first_then_heartbeat(rig):
    assert wait_for(lambda: len(rig.pi.got) >= 6)
    names = rig.pi.names()
    assert names[0] == m.TIME_SYNC
    start = len(rig.pi.got)
    time.sleep(1.0)
    hb = rig.pi.names()[start:].count(m.HEARTBEAT)
    assert 3 <= hb <= 7                               # 0.2초마다 → 1초에 약 5번


def test_reconnect_after_drop(rig):
    assert wait_for(lambda: rig.pi.connects == 1 and rig.pi.got)
    rig.pi.drop()
    assert wait_for(lambda: any(e.get("code") == "car_link_lost" for e in rig.log.of("alert")))
    t0 = time.monotonic()
    assert wait_for(lambda: rig.pi.connects == 2, timeout=3.0)
    assert time.monotonic() - t0 < 2.0                # 1초마다 다시 접속
    n = rig.pi.conn_start[1]
    assert wait_for(lambda: len(rig.pi.got) > n)
    assert rig.pi.names()[n] == m.TIME_SYNC           # 다시 접속해도 time_sync가 먼저


def test_silent_pi_counts_as_lost(rig):
    """파이가 연결은 둔 채 아무것도 보내지 않으면(와이파이가 조용히 끊긴 경우) 끊김으로 봄."""
    assert wait_for(lambda: rig.pi.connects == 1)
    assert wait_for(lambda: any(e.get("code") == "car_link_lost" for e in rig.log.of("alert")), timeout=3.0)


def test_ws_state_and_cmd(rig):
    with rig.ws() as ws:
        first = json.loads(ws.recv(timeout=3))
        assert first["topic"] == m.UI_STATE
        m.validate(first)
        rig.cmd(ws, "select_car", car=2)
        st = recv_until(ws, lambda x: x["topic"] == m.UI_STATE and x["selected_car"] == 2)
        assert st is not None


def test_ws_bad_cmd_alert_and_survive(rig):
    with rig.ws() as ws:
        ws.recv(timeout=3)
        ws.send("이건 JSON이 아님")
        alert = recv_until(ws, lambda x: x["topic"] == m.UI_ALERT)
        assert alert is not None and alert["code"] == "cmd_rejected"
        rig.cmd(ws, "select_car", car=2)
        assert recv_until(ws, lambda x: x["topic"] == m.UI_STATE and x["selected_car"] == 2)
    assert any(e.get("source") == "ui" for e in rig.log.of("bad_msg"))


def test_hand_pose_goes_to_pi_udp(rig):
    assert wait_for(lambda: rig.pi.conn is not None and rig.pi.got)
    rig.pi.send(m.STATUS, state="arrived", station="wait_1", moving_to=None, serial_ok=True)
    with rig.ws() as ws:
        ws.recv(timeout=3)
        rig.cmd(ws, "select_car", car=1)
        rig.cmd(ws, "set_mode", car=1, mode="manual")
        assert recv_until(ws, lambda x: x["topic"] == m.UI_STATE and x["cars"]["1"]["mode"] == "manual")
        for i in range(5):                            # 손 인식 → 관제 → 파이
            rig.send_hand_pose([0.2 * i] * 5, 0.0)
            time.sleep(0.05)
        assert wait_for(lambda: rig.pi.hands)
        h = rig.pi.hands[-1]
        assert h["topic"] == "car1/hand" and len(h["angles"]) == 6


def test_bad_messages_do_not_kill_service(rig):
    assert wait_for(lambda: rig.pi.conn is not None and rig.pi.got)
    rig.udp.sendto(b"\xff\xfe garbage", ("127.0.0.1", rig.net["laptop"]["hand_pose_port"]))
    vision = m.Sender().make(m.VISION_STATE, slots={"1": "occupied", "2": "empty", "3": "empty"},
                             bench="empty", cock_led=False, fps=30.0)
    rig.udp.sendto(m.encode(vision), ("127.0.0.1", rig.net["laptop"]["hand_pose_port"]))  # 다른 포트로 온 topic
    rig.pi.send_raw(b"{not json}\n")
    assert wait_for(lambda: len(rig.log.of("bad_msg")) >= 3)
    rig.pi.send(m.STATUS, state="arrived", station="wait_1", moving_to=None, serial_ok=True)
    with rig.ws() as ws:                              # 서비스가 살아 있음
        st = recv_until(ws, lambda x: x["topic"] == m.UI_STATE and x["cars"]["1"]["pi_state"] == "arrived")
        assert st is not None


# ---------------------------------------------------------------------------
# --network 옵션: 노트북 안 가짜 작업차에 접속 (6단계)
# ---------------------------------------------------------------------------

LOCAL_NETWORK = config.ROOT / "tests" / "fakes" / "network_local.yaml"


def test_network_option_default_and_local():
    from laptop.control_service.main import parse_args
    assert parse_args([]).network == config.NETWORK_YAML
    assert parse_args(["--network", str(LOCAL_NETWORK)]).network == LOCAL_NETWORK


def test_local_network_cars_on_this_laptop_with_own_ports():
    net = config.load_network(LOCAL_NETWORK)            # 형식이 틀리면 여기서 오류
    cars = [net["cars"][str(n)] for n in m.CAR_IDS]
    assert all(c["ip"] == "127.0.0.1" for c in cars)
    ports = [c[k] for c in cars for k in ("hand_port", "cmd_port")]
    assert len(set(ports)) == len(ports)                 # 두 작업차가 한 노트북에서 포트를 나눠 씀


def test_local_network_same_timing_and_laptop_ports_as_real():
    real, local = config.load_network(), config.load_network(LOCAL_NETWORK)
    assert local["timing"] == real["timing"]
    assert {k: v for k, v in local["laptop"].items() if k != "ip"} == \
           {k: v for k, v in real["laptop"].items() if k != "ip"}
