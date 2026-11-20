"""
관제 서비스 (노트북) — 담당: 이보연

하는 일: 진행 관리(작업차 모드, 작업대 상태, 혼자 하기), 자세 변환(손 → 서보 각도),
        가르치기 기록·재생, 웹 서버(FastAPI, :8000, WebSocket /ws), 로그.
입력: hand/pose, vision/state, 파이 보고(TCP 5006), ui/cmd, data/ 파일
출력: car{n}/hand (UDP 5005), 파이 명령(TCP 5006), ui/state, ui/alert, hand/calib, data/ 파일
관련 SR: SR-02~04, SR-07, SR-08, SR-18~22, SR-24, SR-27~40
참고: 시스템 설계 6~9장, 인터페이스 정의 4·6장

이 파일은 네트워크와 core.Control 사이를 잇기만 한다. 무엇을 할지는 모두 core가 정한다.
asyncio 하나로 돌므로 core는 한 번에 한 곳에서만 불린다 (잠금 필요 없음).

    받기  → core.on_*(msg, now)
    주기  → core.tick(now)                  (TICK_S마다)
    보내기 ← core.take_outbox()              (받거나 tick한 직후 바로)
    ui/state → 브라우저                     (STATE_PERIOD_S마다 + core.dirty일 때 바로)

실행: python -m laptop.control_service.main   (저장소 맨 위 폴더에서)
"""

from __future__ import annotations

import asyncio
import pathlib
import socket
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from shared import config
from shared import messages as m

from .core import Control
from .logbook import Logbook
from .tasks import TaskStore

WEB_DIR = pathlib.Path(__file__).resolve().parent / "web"
TICK_S = 0.01               # 재생 프레임 간격(기록 때 약 33ms)을 맞출 만큼 짧게
STATE_PERIOD_S = 0.2        # ui/state 1초 5번 (인터페이스 정의 6.1)
CONNECT_TIMEOUT_S = 1.0
SILENT_FACTOR = 3           # 파이 status(0.5초마다)가 이 배수만큼 안 오면 연결이 죽은 것으로 봄


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, on_data: Callable[[bytes], None]) -> None:
        self.on_data = on_data

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.on_data(data)


class ControlService:
    """관제 서비스 하나. run()을 asyncio로 돌리거나, 시험에서는 start_thread()/stop()."""

    def __init__(self, net: Dict[str, Any], mp: config.MapConfig, calibs: Dict[int, Optional[Dict[str, Any]]],
                 store: TaskStore, log: Any, host: str = "127.0.0.1") -> None:
        self.net = net
        self.log = log
        self.host = host
        self.core = Control(net, mp, calibs, store, log)
        self.timing = net["timing"]
        self.writers: Dict[int, Optional[asyncio.StreamWriter]] = {n: None for n in m.CAR_IDS}
        self.clients: Set[WebSocket] = set()
        self.seq = {m.HAND_POSE: m.SeqFilter(), m.VISION_STATE: m.SeqFilter()}
        self.udp_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.server: Optional[uvicorn.Server] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.app = self._make_app()

    # ------------------------------------------------------------------ 보내기
    async def flush(self) -> None:
        """core가 쌓은 것을 보내고, 바뀐 게 있으면 ui/state를 바로 보냄."""
        for kind, n, msg in self.core.take_outbox():
            if kind == "tcp":
                self._write_tcp(n, msg)
            elif kind == "udp":
                car = self.net["cars"][str(n)]
                self._sendto(msg, car["ip"], car["hand_port"])
            elif kind == "calib":
                self._sendto(msg, "127.0.0.1", self.net["laptop"]["hand_calib_port"])
            elif kind == "alert":
                await self.broadcast(msg)
        if self.core.dirty:
            await self.send_state()

    def _write_tcp(self, n: Optional[int], msg: Dict[str, Any]) -> None:
        w = self.writers.get(n) if n is not None else None
        if w is None or w.is_closing():
            return                                  # 연결이 없으면 버림 (core는 연결됐을 때만 명령을 만듦)
        try:
            w.write(m.encode(msg))
        except (OSError, RuntimeError):
            pass                                    # 끊김은 읽는 쪽에서 알아채고 처리

    def _sendto(self, msg: Dict[str, Any], ip: str, port: int) -> None:
        try:
            self.udp_out.sendto(m.encode(msg), (ip, port))
        except OSError as e:
            self.log.event("send_fail", topic=msg["topic"], error=str(e))

    async def send_state(self) -> None:
        self.core.dirty = False
        await self.broadcast(self.core.ui_state())

    async def broadcast(self, msg: Dict[str, Any]) -> None:
        text = m.encode(msg).decode("utf-8")
        for ws in list(self.clients):
            try:
                await ws.send_text(text)
            except Exception:                       # 브라우저가 닫힘
                self.clients.discard(ws)

    # ------------------------------------------------------------------ 받기: UDP (손 인식, 맵 비전)
    def _on_udp(self, topic: str, data: bytes) -> None:
        try:
            msg = m.decode(data)
        except m.MessageError as e:
            self.log.event("bad_msg", source=topic, error=str(e))
            return
        if msg["topic"] != topic:
            self.log.event("bad_msg", source=topic, error=f"이 포트로 오면 안 되는 topic: {msg['topic']}")
            return
        if not self.seq[topic].accept(msg):
            return                                  # 늦게 도착한 옛 메시지 (인터페이스 정의 3장)
        now = time.monotonic()
        if topic == m.HAND_POSE:
            self.core.on_hand_pose(msg, now)
        else:
            self.core.on_vision(msg, now)
        asyncio.ensure_future(self.flush())

    # ------------------------------------------------------------------ 받기: TCP (파이)
    async def car_link(self, n: int) -> None:
        """작업차 하나: 접속 → 읽기 → 끊기면 reconnect_s 뒤 다시 접속 (설계 11장)."""
        car = self.net["cars"][str(n)]
        silent_s = SILENT_FACTOR * self.timing["status_period_s"]
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(car["ip"], car["cmd_port"]), CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(self.timing["reconnect_s"])
                continue
            self.writers[n] = writer
            self.core.on_link(n, True, time.monotonic())
            await self.flush()                      # time_sync가 heartbeat보다 먼저 나감
            hb = asyncio.ensure_future(self._heartbeat(n, writer))
            lines = m.LineReader()
            try:
                while True:
                    data = await asyncio.wait_for(reader.read(4096), silent_s)
                    if not data:
                        break                       # 파이가 연결을 닫음
                    for line in lines.feed(data):
                        self._on_car_line(n, line)
                    await self.flush()
            except (OSError, asyncio.TimeoutError, m.MessageError) as e:
                self.log.event("link_error", car=n, error=type(e).__name__)
            finally:
                hb.cancel()
                self.writers[n] = None
                writer.close()
                self.core.on_link(n, False, time.monotonic())
                await self.flush()
            await asyncio.sleep(self.timing["reconnect_s"])

    def _on_car_line(self, n: int, line: bytes) -> None:
        try:
            msg = m.decode(line)
        except m.MessageError as e:
            self.log.event("bad_msg", source=f"car{n}", error=str(e))
            return
        self.core.on_car_msg(n, msg, time.monotonic())

    async def _heartbeat(self, n: int, writer: asyncio.StreamWriter) -> None:
        while not writer.is_closing():
            self._write_tcp(n, self.core.out.make(m.car_topic(n, m.HEARTBEAT)))
            await asyncio.sleep(self.timing["heartbeat_s"])

    # ------------------------------------------------------------------ 주기
    async def ticker(self) -> None:
        while True:
            self.core.tick(time.monotonic())
            await self.flush()
            await asyncio.sleep(TICK_S)

    async def state_loop(self) -> None:
        while True:
            await asyncio.sleep(STATE_PERIOD_S)
            await self.send_state()

    # ------------------------------------------------------------------ 웹 (조종 화면)
    def _make_app(self) -> FastAPI:
        app = FastAPI()

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            self.clients.add(ws)
            try:
                await ws.send_text(m.encode(self.core.ui_state()).decode("utf-8"))
                while True:
                    text = await ws.receive_text()
                    await self._on_ui(text)
            except WebSocketDisconnect:
                pass
            finally:
                self.clients.discard(ws)

        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
        return app

    async def _on_ui(self, text: str) -> None:
        try:
            msg = m.decode(text)
            if msg["topic"] != m.UI_CMD:
                raise m.MessageError(f"브라우저가 보낼 수 없는 topic: {msg['topic']}")
        except m.MessageError as e:
            self.log.event("bad_msg", source="ui", error=str(e))
            self.core.alert("warn", None, "cmd_rejected", f"할 수 없는 명령: 형식 오류 ({e})")
            await self.flush()
            return
        self.core.on_ui_cmd(msg, time.monotonic())
        await self.flush()

    # ------------------------------------------------------------------ 실행
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        lp = self.net["laptop"]
        await self.loop.create_datagram_endpoint(
            lambda: _Udp(lambda d: self._on_udp(m.HAND_POSE, d)), local_addr=("127.0.0.1", lp["hand_pose_port"]))
        await self.loop.create_datagram_endpoint(
            lambda: _Udp(lambda d: self._on_udp(m.VISION_STATE, d)), local_addr=("127.0.0.1", lp["vision_state_port"]))
        tasks: List[asyncio.Future] = [asyncio.ensure_future(self.car_link(n)) for n in m.CAR_IDS]
        tasks += [asyncio.ensure_future(self.ticker()), asyncio.ensure_future(self.state_loop())]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host=self.host, port=lp["web_port"],
                                                    log_level="warning", lifespan="off"))
        watcher = asyncio.ensure_future(self._mark_started())
        try:
            await self.server.serve()
        finally:
            for t in tasks + [watcher]:
                t.cancel()
            await asyncio.gather(*tasks, watcher, return_exceptions=True)
            self.udp_out.close()

    async def _mark_started(self) -> None:
        while self.server is not None and not self.server.started:
            await asyncio.sleep(0.01)
        self._started.set()

    # 시험용: 다른 스레드에서 돌리고 멈추기
    def start_thread(self, timeout: float = 5.0) -> None:
        self._thread = threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise RuntimeError("관제 서비스가 시작되지 않음")

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self._thread is not None:
            self._thread.join(5.0)


def load_calibs(net: Dict[str, Any]) -> Dict[int, Optional[Dict[str, Any]]]:
    """작업차별 AiHand 보정 파일. 없으면 그 작업차만 손 조종을 막고 뜸 (인터페이스 정의 9.9)."""
    calibs = {}
    for n in m.CAR_IDS:
        name = net["cars"][str(n)]["aihand"]
        calibs[n] = config.load_aihand(name)
        if calibs[n] is None:
            print(f"[경고] 작업차 {n}: data/calibration/{name}.json 없음 → 이 작업차는 손 조종·혼자 하기를 막음")
    return calibs


def main() -> None:
    net = config.load_network()                     # 틀리면 여기서 오류를 내고 멈춤
    mp = config.load_map()
    calibs = load_calibs(net)
    log = Logbook()
    svc = ControlService(net, mp, calibs, TaskStore(), log)
    print(f"관제 서비스: http://localhost:{net['laptop']['web_port']}  (로그 {log.folder})")
    try:
        asyncio.run(svc.run())
    except KeyboardInterrupt:
        pass
    finally:
        log.close()
        print("관제 서비스 끝")


if __name__ == "__main__":
    sys.exit(main())
