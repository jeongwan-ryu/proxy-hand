"""
관제 서비스의 판단 부분 (네트워크 없음). main.py가 받은 메시지를 넣고, 보낼 것을 꺼내 간다.

    core = Control(net, mp, calibs, store, log)
    core.on_hand_pose(msg, now) / on_vision(msg, now) / on_car_msg(car, msg, now)
    core.on_link(car, up, now) / on_ui_cmd(msg, now) / tick(now)
    for kind, car, msg in core.take_outbox(): ...   # kind: "tcp" / "udp" / "calib" / "alert"
    core.ui_state()

now는 time.monotonic() 기준 초. 규칙은 시스템 설계 5.2(보류), 6장(모드·작업대), 7장(혼자 하기),
8장(가르치기·재생), 9.3·9.5(자세), 11장(오류), 12장(버튼 조건)을 따른다.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from shared import config
from shared import messages as m

from . import pose
from .tasks import STORAGE_ANY, Recorder, TaskError, TaskStore, build_plan, uses_storage

REPLAY_TAIL_REPEAT = 2          # hand 단계 마지막 프레임을 0.1초 간격으로 2번 더 (총 3번, 설계 8장)
REPLAY_TAIL_GAP_S = 0.1
LIVE_MS_DEFAULT = 33
DRIVE_POSE_MS = 300

ERROR_TEXT = {
    "line_lost": "선 놓침",
    "line_missed": "정지선 놓침",
    "serial_fail": "AiHand 연결 오류",
}

MODE_NAMES = {"idle": "대기", "manual": "조종", "teach": "가르치기", "auto": "혼자 하기", "stopped": "정지"}


class HeldMarker:
    """2단계 판단 (설계 5.2): 작업차가 가렸을 수 있으면 unknown(보류).
    보류가 풀린 뒤에는 맵 비전 값이 hold_settle_s 동안 그대로여야 그 값을 씀."""

    def __init__(self, settle_s: float) -> None:
        self.settle_s = settle_s
        self.held = False
        self.settle_from: Optional[float] = None
        self.last_raw = "unknown"
        self.value = "unknown"

    def update(self, raw: str, held: bool, now: float) -> str:
        if held:
            self.held = True
            self.value = "unknown"
            return self.value
        if self.held:
            self.held = False
            self.settle_from, self.last_raw = now, raw
        if self.settle_from is not None:
            if raw != self.last_raw:
                self.settle_from, self.last_raw = now, raw
            if now - self.settle_from < self.settle_s:
                self.value = "unknown"
                return self.value
            self.settle_from = None
        self.last_raw = raw
        self.value = raw
        return raw


class Run:
    """혼자 하기 한 번 (반복 포함)."""

    def __init__(self, task_id: str, task: Dict[str, Any], plan: List[Dict[str, Any]], repeat: bool) -> None:
        self.task_id = task_id
        self.task = task
        self.plan = plan
        self.repeat = repeat
        self.cycles_done = 0
        self.phase = "wait_cond"        # wait_cond / goto / hand
        self.next_check = 0.0
        self.idx = 0
        self.storage: Optional[str] = None     # 이번 반복에서 고른 칸 정거장
        self.goto_target: Optional[str] = None
        self.frame = 0
        self.tail = 0
        self.next_at = 0.0
        self.last_angles: Optional[List[int]] = None
        self.started_at = 0.0


class Car:
    def __init__(self, n: int, aihand_name: str, calib: Optional[Dict[str, Any]], video_url: str) -> None:
        self.n = n
        self.aihand_name = aihand_name
        self.calib = calib
        self.video_url = video_url
        self.mode = "idle"
        self.link = False
        self.ever_linked = False
        self.pi_state: Optional[str] = None
        self.station: Optional[str] = None
        self.moving_to: Optional[str] = None
        self.between = False                    # 정거장 사이 (driving 뒤 아직 arrived 안 함)
        self.last_angles: Optional[List[int]] = None
        self.last_hand_t: Optional[float] = None
        self.recorder: Optional[Recorder] = None
        self.run: Optional[Run] = None
        self.cycle = 0
        self.repeat = False
        self.result_pending = False
        self.result_cycle: Optional[int] = None
        self.result_task: Optional[str] = None
        self.pending_goto: Optional[Tuple[str, float]] = None
        self.goto_target: Optional[str] = None
        self.goto_deadline: Optional[float] = None
        self.pending_set_station: Optional[str] = None
        self.auto_finished_at: Optional[float] = None


class Control:
    def __init__(self, net: Dict[str, Any], mp: config.MapConfig, calibs: Dict[int, Optional[Dict[str, Any]]],
                 store: TaskStore, log: Any, sender: Optional[m.Sender] = None) -> None:
        self.net = net
        self.mp = mp
        self.store = store
        self.log = log
        self.out = sender or m.Sender()
        self.outbox: List[Tuple[str, Optional[int], Dict[str, Any]]] = []
        self.timing = net["timing"]
        self.cars: Dict[int, Car] = {}
        for n in m.CAR_IDS:
            c = net["cars"][str(n)]
            url = f"http://{c['ip']}:{c['video_port']}/video"
            self.cars[n] = Car(n, c["aihand"], calibs.get(n), url)
        self.selected = 1
        lp = net["laptop"]
        self.map_video_url = f"http://localhost:{lp['map_video_port']}/video"
        self.hand_video_url = f"http://localhost:{lp['hand_video_port']}/video"

        # 손 인식
        self.hand_up = False
        self.hand_last: Optional[float] = None
        self.hand_detected = False
        self.hand_calibrated = False
        self.hand_fingers: Optional[List[float]] = None
        self.hand_wrist: Optional[float] = None
        self.hand_prev_t: Optional[float] = None

        # 맵 비전
        self.vision_up = False
        self.vision_last: Optional[float] = None
        self.v_slots = {k: "unknown" for k in m.SLOT_KEYS}
        self.v_bench = "unknown"
        self.cock_led: Optional[bool] = None
        settle = mp.vision["hold_settle_s"]
        self.slot_view = {k: HeldMarker(settle) for k in m.SLOT_KEYS}
        self.bench_view = HeldMarker(settle)

        # 작업대 (설계 6.3)
        self.bench = "empty"
        self.bench_init = False
        self.bench_leftover = False
        self.hold_mem: Optional[Dict[str, bool]] = None
        self.cock_open_latched = False

        # 정거장 → 그 정거장을 쓰는 작업차, 칸 번호 → 정거장
        self.car_stations = {n: set(mp.stations_of_car(n)) for n in m.CAR_IDS}
        self.slot_station = {str(s["slot"]): sid for sid, s in mp.stations.items() if "slot" in s}
        self.bench_stations = {sid for sid in mp.stations if sid.startswith("bench_")}
        self.cock_car = next((n for n in m.CAR_IDS if "cock" in self.car_stations[n]), 1)
        self.dirty = True       # ui/state를 바로 다시 보낼지

    # ------------------------------------------------------------------ 출력

    def take_outbox(self) -> List[Tuple[str, Optional[int], Dict[str, Any]]]:
        out, self.outbox = self.outbox, []
        return out

    def _tcp(self, car: Car, name: str, **fields: Any) -> None:
        msg = self.out.make(m.car_topic(car.n, name), **fields)
        self.outbox.append(("tcp", car.n, msg))
        if name != m.HEARTBEAT:
            self.log.event("cmd", car=car.n, topic=msg["topic"],
                           fields={k: v for k, v in msg.items() if k not in ("topic", "seq", "t")})

    def _hand(self, car: Car, angles: List[int], ms: int, src: str) -> None:
        ms = max(0, min(1000, int(ms)))
        msg = self.out.make(m.car_topic(car.n, m.HAND), angles=list(angles), ms=ms)
        self.outbox.append(("udp", car.n, msg))
        self.log.hand(car.n, list(angles), ms, src)
        car.last_angles = list(angles)

    def alert(self, level: str, car: Optional[int], code: str, text: str) -> None:
        msg = self.out.make(m.UI_ALERT, level=level, car=car, code=code, text=text)
        self.outbox.append(("alert", car, msg))
        self.log.event("alert", level=level, car=car, code=code, text=text)
        self.dirty = True

    def _reject(self, car: Optional[int], text: str) -> None:
        self.alert("warn", car, "cmd_rejected", f"할 수 없는 명령: {text}")

    # ------------------------------------------------------------------ 모드

    def _set_mode(self, car: Car, new: str, cause: str) -> None:
        if car.mode == new:
            return
        self.log.event("mode_change", car=car.n, **{"from": car.mode, "to": new, "cause": cause})
        car.mode = new
        self.dirty = True

    def _abort_activity(self, car: Car, reason: str) -> None:
        """혼자 하기·가르치기·보내려던 goto를 끊음."""
        if car.run is not None:
            if car.run.phase != "wait_cond":
                self.log.event("task_end", car=car.n, task=car.run.task["name"], cycle=car.cycle, end_reason="stopped")
            car.run = None
        if car.recorder is not None:
            self.log.event("record_discard", car=car.n, steps=len(car.recorder.steps), cause=reason)
            car.recorder = None
        car.pending_goto = None
        car.goto_deadline = None
        car.goto_target = None

    def stop_car(self, car: Car, reason: str, cause: Optional[str] = None, send: bool = True) -> None:
        """정지 (설계 6.1): 바퀴 멈춤, 손 자세 유지. 그 작업차만."""
        self._abort_activity(car, cause or reason)
        if send and car.link:
            self._tcp(car, m.STOP, reason=reason)
        self._set_mode(car, "stopped", cause or reason)

    # ------------------------------------------------------------------ 손 인식 (hand/pose)

    def on_hand_pose(self, msg: Dict[str, Any], now: float) -> None:
        prev_t, self.hand_prev_t = self.hand_prev_t, now
        self.hand_last = now
        if not self.hand_up:
            self.hand_up = True
            self.log.event("service", name="hand", up=True)
            self.dirty = True
        was = self.hand_detected
        self.hand_detected = msg["detected"]
        if self.hand_calibrated != msg["calibrated"]:
            self.hand_calibrated = msg["calibrated"]
            self.dirty = True
        self.hand_fingers, self.hand_wrist = msg["fingers"], msg["wrist"]
        if was != self.hand_detected:
            self.dirty = True
            if was and any(c.mode in ("manual", "teach") for c in self.cars.values()):
                self.alert("warn", self.selected, "hand_lost", "손 인식 끊김")
        self._live_hand(now, prev_t)

    def hand_allowed(self, car: Car) -> bool:
        """손 명령 조건 (설계 6.1, SR-07, SR-08)."""
        return (car.n == self.selected and car.mode in ("manual", "teach") and car.link
                and car.pi_state == "arrived" and car.calib is not None and car.pending_goto is None)

    def _live_hand(self, now: float, prev_t: Optional[float]) -> None:
        car = self.cars[self.selected]
        if not (self.hand_up and self.hand_detected and self.hand_calibrated and self.hand_allowed(car)):
            return
        if self.hand_fingers is None or self.hand_wrist is None:
            return
        angles = pose.hand_to_servos(self.hand_fingers, self.hand_wrist, car.calib)  # type: ignore[arg-type]
        if not pose.changed_enough(angles, car.last_angles):
            return
        ms = LIVE_MS_DEFAULT if prev_t is None else max(10, min(200, int(round((now - prev_t) * 1000))))
        self._hand(car, angles, ms, "live")
        if car.mode == "teach" and car.recorder is not None:
            car.recorder.hand(angles, now)
            self.dirty = True

    # ------------------------------------------------------------------ 맵 비전 (vision/state)

    def on_vision(self, msg: Dict[str, Any], now: float) -> None:
        self.vision_last = now
        if not self.vision_up:
            self.vision_up = True
            self.log.event("service", name="vision", up=True)
            self.dirty = True
        if msg["slots"] != self.v_slots or msg["bench"] != self.v_bench or msg["cock_led"] != self.cock_led:
            self.dirty = True
        self.v_slots = dict(msg["slots"])
        self.v_bench = msg["bench"]
        self.cock_led = msg["cock_led"]

    def _car_hides(self, car: Car, stations: set) -> bool:
        """이 작업차의 AiHand가 stations의 마커를 가렸을 수 있나 (설계 5.2 보류 규칙)."""
        if not car.link or car.pi_state is None:
            return False
        if not (stations & self.car_stations[car.n]):
            return False                            # 다른 고리: 그 마커에 닿지 않음
        if car.pi_state == "driving":
            return True
        if car.between and car.pi_state in ("stopped", "error"):
            return True                             # 정거장 사이에서 멈춤
        return not car.between and car.station in stations

    def _update_views(self, now: float) -> None:
        for k in m.SLOT_KEYS:
            st = {self.slot_station[k]} if k in self.slot_station else set()
            held = any(self._car_hides(c, st) for c in self.cars.values())
            before = self.slot_view[k].value
            if self.slot_view[k].update(self.v_slots[k], held, now) != before:
                self.dirty = True
        held = any(self._car_hides(c, self.bench_stations) for c in self.cars.values())
        before = self.bench_view.value
        if self.bench_view.update(self.v_bench, held, now) != before:
            self.dirty = True

    def slots(self) -> Dict[str, str]:
        return {k: self.slot_view[k].value for k in m.SLOT_KEYS}

    # ------------------------------------------------------------------ 작업대 (설계 6.3)

    def _set_bench(self, new: str) -> None:
        if new != self.bench:
            self.log.event("bench_change", **{"from": self.bench, "to": new})
            self.bench = new
            self.dirty = True

    def _cock_open(self) -> None:
        if self.cock_open_latched:
            return
        self.cock_open_latched = True
        self.alert("error", self.cock_car, "cock_open", "콕이 열려 있음 (바이알을 놓을 때 콕 LED가 이미 켜짐)")
        car = self.cars[self.cock_car]
        if car.mode != "stopped":
            self.stop_car(car, "cock_open")

    def _update_bench(self) -> None:
        vial = self.bench_view.value
        led = self.cock_led is True
        if not self.bench_init:
            if self.v_bench == "unknown" or vial == "unknown":
                return
            self.bench_init = True
            if vial == "occupied":
                self.bench_leftover = True
                self.alert("warn", None, "bench_not_empty", "작업대에 바이알이 남아 있음 (치우면 경고가 사라짐)")
            return
        if self.bench_leftover:
            if vial == "empty":
                self.bench_leftover = False
                self.log.event("bench_leftover_cleared")
                self.dirty = True
            return
        if vial == "unknown":                       # 보류 중: 상태는 그대로, LED는 기억 (6.3)
            if self.bench == "empty":
                if self.hold_mem is None:
                    self.hold_mem = {"led_start": led, "seen": False}
                elif led:
                    self.hold_mem["seen"] = True
            elif self.bench == "filling" and led:
                self._set_bench("filled")
            return
        mem, self.hold_mem = self.hold_mem, None
        if vial == "empty" or not led:
            self.cock_open_latched = False
        if self.bench == "empty":
            if vial != "occupied":
                return
            if mem is not None:
                if mem["led_start"]:
                    self._cock_open()
                elif mem["seen"] or led:
                    self._set_bench("filled")
                else:
                    self._set_bench("filling")
            elif led:
                self._cock_open()
            else:
                self._set_bench("filling")
        elif self.bench == "filling":
            if led:
                self._set_bench("filled")
            elif vial == "empty":
                self._set_bench("empty")
        elif self.bench == "filled" and vial == "empty":
            self._set_bench("empty")

    def bench_ready(self) -> bool:
        return self.bench_init and not self.bench_leftover and self.bench == "empty"

    # ------------------------------------------------------------------ 파이 (TCP)

    def on_link(self, n: int, up: bool, now: float) -> None:
        car = self.cars[n]
        if up == car.link:
            return
        car.link = up
        self.dirty = True
        self.log.event("link", car=n, up=up)
        if up:
            car.ever_linked = True
            self._tcp(car, m.TIME_SYNC, now=round(time.time(), 3))
            return
        car.pi_state = None
        self.alert("error", n, "car_link_lost", f"작업차 {n} 연결 끊김")
        if car.mode != "stopped":
            # 대기여도 정지로 (설계 11장). 파이는 끊긴 동안 스스로 stopped가 되므로,
            # 다시 연결된 뒤 [조종]이 resume을 보내야 파이가 arrived로 돌아와 손 명령을 받음
            self.stop_car(car, "car_link_lost", send=False)
        else:
            self._abort_activity(car, "car_link_lost")

    def on_car_msg(self, n: int, msg: Dict[str, Any], now: float) -> None:
        car = self.cars[n]
        parsed = m.split_car_topic(msg["topic"])
        if parsed is None or parsed[0] != n:
            self.log.event("ignored", car=n, topic=msg["topic"])
            return
        name = parsed[1]
        fields = {k: v for k, v in msg.items() if k not in ("topic", "seq", "t")}
        if name == m.STATUS:
            changed = msg["state"] != car.pi_state
            if changed:
                self.log.event("report", car=n, topic=msg["topic"], fields=fields)
                self._pi_state(car, msg["state"])
            if (car.station, car.moving_to) != (msg["station"], msg["moving_to"]):
                self.dirty = True
            car.station, car.moving_to = msg["station"], msg["moving_to"]
            return
        self.log.event("report", car=n, topic=msg["topic"], fields=fields)
        if name == m.ARRIVED:
            self._pi_state(car, "arrived")
            car.station, car.moving_to = msg["station"], None
            self._on_arrived(car, msg["station"], now)
        elif name == m.ERROR:
            code = msg["code"]
            if code == "link_lost":
                return                                  # 파이가 스스로 멈췄던 기록 (화면 표시 없음, 설계 11장)
            self._pi_state(car, "error")
            self.alert("error", n, code, f"작업차 {n} {ERROR_TEXT.get(code, code)}: {msg['text']}")
            if car.mode != "stopped":
                self.stop_car(car, code, send=False)
            else:
                self._abort_activity(car, code)
        elif name == m.REJECT:
            self._reject(n, f"작업차 {n} {msg['cmd']} ({msg['text']})")
            cmd = m.split_car_topic(msg["cmd"])
            if car.run is not None and cmd is not None and cmd[1] == m.GOTO:
                self.stop_car(car, "cmd_rejected")
            elif cmd is not None and cmd[1] == m.GOTO:
                car.goto_deadline = car.goto_target = None
            elif cmd is not None and cmd[1] == m.SET_STATION:
                car.pending_set_station = None

    def _pi_state(self, car: Car, state: str) -> None:
        if state == car.pi_state:
            return
        car.pi_state = state
        if state == "driving":
            car.between = True
        elif state == "arrived":
            car.between = False
        self.dirty = True

    def _on_arrived(self, car: Car, station: str, now: float) -> None:
        if car.goto_target == station:
            car.goto_target = car.goto_deadline = None
        if car.pending_set_station == station:
            car.pending_set_station = None
            if car.calib is not None:
                self._hand(car, pose.initial_pose(car.calib), DRIVE_POSE_MS, "initial")
        r = car.run
        if r is not None and r.phase == "goto" and r.goto_target == station:
            r.idx += 1
            self._begin_step(car, now)

    # ------------------------------------------------------------------ 이동 (주행 자세 → goto)

    def _request_goto(self, car: Car, station: str, now: float, src: str) -> None:
        dp = self.mp.drive_pose
        if dp["enabled"] and car.calib is not None:
            self._hand(car, pose.drive_pose(car.last_angles, car.calib), DRIVE_POSE_MS, "drive")
            car.pending_goto = (station, now + dp["settle_s"])
        else:
            car.pending_goto = (station, now)
        if car.mode == "teach" and car.recorder is not None:
            car.recorder.goto(station)
        self.dirty = True
        if car.pending_goto[1] <= now:
            self._flush_goto(car, now)

    def _flush_goto(self, car: Car, now: float) -> None:
        station, _ = car.pending_goto  # type: ignore[misc]
        car.pending_goto = None
        car.goto_target = station
        car.goto_deadline = now + self.mp.auto["goto_timeout_s"]
        self._tcp(car, m.GOTO, station=station)

    # ------------------------------------------------------------------ 혼자 하기 (설계 7장)

    def _start_condition(self, car: Car) -> bool:
        when = self.mp.cars[str(car.n)]["start_when"]
        if when == "bench_empty":
            return self.bench_ready()
        return self.bench == "filled"

    def _partner_finished(self, car: Car) -> bool:
        """작업차 2 종료 조건: 작업차 1(start_when=bench_empty)의 반복이 이 작업 시작 뒤 끝났음."""
        r = car.run
        for other in self.cars.values():
            if other is car or self.mp.cars[str(other.n)]["start_when"] != "bench_empty":
                continue
            if other.mode != "auto" and other.auto_finished_at is not None and r is not None \
                    and other.auto_finished_at >= r.started_at:
                return True
        return False

    def _auto_tick(self, car: Car, now: float) -> None:
        r = car.run
        if r is None:
            return
        if r.phase == "wait_cond":
            if now < r.next_check:
                return
            r.next_check = now + self.mp.auto["condition_check_s"]
            if (self.mp.cars[str(car.n)]["start_when"] == "bench_filled" and self._partner_finished(car)
                    and self.bench == "empty" and self.bench_view.value == "empty"):
                self._finish_run(car, now, "storage_empty", "보관함 비었음 (작업차 1 반복 끝, 작업대 빔)")
                return
            if car.pi_state != "arrived":
                return
            if not self._start_condition(car):
                return
            r.storage = None
            if uses_storage(r.plan):
                view = self.slots()
                if any(v == "unknown" for v in view.values()):
                    return                              # 보류: 0.2초 뒤 다시 봄 (설계 5.2)
                full = [k for k in m.SLOT_KEYS if view[k] == "occupied" and k in self.slot_station]
                if not full:
                    if r.cycles_done == 0:
                        self.alert("error", car.n, "vial_not_found", "바이알을 찾지 못함")
                        car.run = None
                        self._set_mode(car, "idle", "vial_not_found")
                    else:
                        self._finish_run(car, now, "storage_empty", "보관함 비었음")
                    return
                r.storage = self.slot_station[full[0]]          # 번호가 가장 작은 칸부터
            self.log.event("task_start", car=car.n, task=r.task["name"], cycle=car.cycle, storage=r.storage)
            r.idx = 0
            self._begin_step(car, now)
            return
        if r.phase == "hand":
            frames = r.plan[r.idx]["frames"]
            while car.run is r and r.phase == "hand" and now >= r.next_at:
                if r.frame < len(frames):
                    f = frames[r.frame]
                    nxt = frames[r.frame + 1]["dt"] if r.frame + 1 < len(frames) else REPLAY_TAIL_GAP_S
                    angles = pose.clamp_all(f["angles"], car.calib) if car.calib else list(f["angles"])
                    self._hand(car, angles, max(LIVE_MS_DEFAULT, int(nxt * 1000)), "replay")
                    r.last_angles = angles
                    r.frame += 1
                    if r.frame >= len(frames):
                        r.tail = REPLAY_TAIL_REPEAT
                    r.next_at += nxt
                elif r.tail > 0:
                    self._hand(car, r.last_angles, LIVE_MS_DEFAULT, "replay")  # type: ignore[arg-type]
                    r.tail -= 1
                    r.next_at += REPLAY_TAIL_GAP_S
                else:
                    r.idx += 1
                    self._begin_step(car, now)

    def _begin_step(self, car: Car, now: float) -> None:
        r = car.run
        if r is None:
            return
        self.dirty = True
        if r.idx >= len(r.plan):
            self._cycle_done(car, now)
            return
        step = r.plan[r.idx]
        if step["type"] == "goto":
            station = step["station"]
            if station == STORAGE_ANY:
                station = r.storage or station
            r.phase, r.goto_target = "goto", station
            if car.station == station and car.pi_state == "arrived" and not car.between:
                self._tcp(car, m.GOTO, station=station)          # 이미 그 정거장: 파이가 바로 arrived를 다시 보냄
                car.goto_target, car.goto_deadline = station, now + self.mp.auto["goto_timeout_s"]
            else:
                self._request_goto(car, station, now, "replay")
        else:
            r.phase, r.frame, r.tail, r.next_at = "hand", 0, 0, now

    def _cycle_done(self, car: Car, now: float) -> None:
        r = car.run
        assert r is not None
        r.cycles_done += 1
        self.log.event("task_end", car=car.n, task=r.task["name"], cycle=car.cycle, end_reason="done")
        car.result_pending, car.result_cycle, car.result_task = True, car.cycle, r.task["name"]
        if r.repeat:
            car.cycle += 1
            r.phase, r.next_check, r.idx = "wait_cond", now, 0
        else:
            car.run = None
            car.auto_finished_at = now
            self._set_mode(car, "idle", "task_done")
            self.alert("info", car.n, "task_done", f"작업차 {car.n} 작업 끝: {r.task['name']}")
        self.dirty = True

    def _finish_run(self, car: Car, now: float, code: str, text: str) -> None:
        r = car.run
        assert r is not None
        self.log.event("task_end", car=car.n, task=r.task["name"], cycle=car.cycle, end_reason=code)
        car.run = None
        car.auto_finished_at = now
        self._set_mode(car, "idle", code)
        self.alert("info", car.n, code, f"작업차 {car.n} {text}")

    # ------------------------------------------------------------------ 주기 처리

    def tick(self, now: float) -> None:
        to = self.timing["service_timeout_s"]
        if self.hand_up and self.hand_last is not None and now - self.hand_last > to:
            self.hand_up = self.hand_detected = False
            self.log.event("service", name="hand", up=False)
            self.alert("error", None, "hand_service_down", "손 인식 서비스 꺼짐")
        if self.vision_up and self.vision_last is not None and now - self.vision_last > to:
            self.vision_up = False
            self.v_slots = {k: "unknown" for k in m.SLOT_KEYS}
            self.v_bench = "unknown"
            self.log.event("service", name="vision", up=False)
            self.alert("error", None, "vision_service_down", "맵 비전 서비스 꺼짐")
            for car in self.cars.values():
                if car.mode == "auto":
                    self.stop_car(car, "vision_service_down")
        self._update_views(now)
        self._update_bench()
        for car in self.cars.values():
            if car.pending_goto is not None and now >= car.pending_goto[1]:
                self._flush_goto(car, now)
            if car.goto_deadline is not None and now > car.goto_deadline:
                self.alert("error", car.n, "goto_timeout", f"작업차 {car.n} 이동 시간 초과")
                self.stop_car(car, "goto_timeout")
            if car.mode == "auto":
                self._auto_tick(car, now)

    # ------------------------------------------------------------------ 버튼 조건 (설계 12장)

    def allowed(self, car: Car) -> List[str]:
        a = ["select_car", "stop", "stop_all"]
        mode, linked = car.mode, car.link
        if mode == "manual":
            a.append("set_mode:idle")
        if mode in ("idle", "stopped"):
            a.append("set_mode:manual")
        if mode in ("idle", "manual"):
            a.append("set_mode:teach")
        if mode == "teach":
            a.append("record_end")
        if mode in ("manual", "teach") and linked and car.pi_state in ("arrived", "stopped") \
                and car.pending_goto is None and car.goto_target is None:
            a.append("goto")
        if mode in ("idle", "stopped") and linked:
            a.append("set_station")
        if (mode == "idle" and linked and car.pi_state == "arrived" and not car.between
                and car.station == self.mp.wait_station(car.n)):
            a.append("start")
        if car.result_pending:
            a.append("mark_result")
        if mode != "auto":
            a.append("delete_task")
        if self.hand_up and self.hand_detected:
            a.append("hand_calib")
        return a

    # ------------------------------------------------------------------ 화면 명령 (ui/cmd)

    def on_ui_cmd(self, msg: Dict[str, Any], now: float) -> None:
        action = msg["action"]
        self.log.event("ui_cmd", **{k: v for k, v in msg.items() if k not in ("topic", "seq", "t")})
        if action == "stop_all":                         # 스페이스바: 무엇보다 먼저 (SR-35)
            for car in self.cars.values():
                self.stop_car(car, "space")
            return
        if action == "hand_calib":
            if not self.hand_up or (msg["step"] != "reset" and not self.hand_detected):
                return self._reject(None, "손 맞춤 (손 인식 서비스 연결과 손이 보여야 함)")
            self.outbox.append(("calib", None, self.out.make(m.HAND_CALIB, step=msg["step"])))
            return
        if action == "delete_task":
            if any(c.run is not None and c.run.task_id == msg["task"] for c in self.cars.values()):
                return self._reject(None, "실행 중인 작업은 지울 수 없음")
            try:
                self.store.delete(msg["task"])
            except TaskError as e:
                return self._reject(None, str(e))
            self.log.event("task_delete", task=msg["task"])
            self.dirty = True
            return
        car = self.cars[msg["car"]]
        key = f"set_mode:{msg['mode']}" if action == "set_mode" else action
        if key not in self.allowed(car):
            return self._reject(car.n, f"작업차 {car.n} {MODE_NAMES[car.mode]} 모드에서 {key}")
        getattr(self, "_ui_" + action)(car, msg, now)
        self.dirty = True

    def _ui_select_car(self, car: Car, msg: Dict[str, Any], now: float) -> None:
        self.selected = car.n

    def _ui_stop(self, car: Car, msg: Dict[str, Any], now: float) -> None:
        self.stop_car(car, "button")

    def _ui_set_mode(self, car: Car, msg: Dict[str, Any], now: float) -> None:
        new = msg["mode"]
        if new == "manual" and car.mode == "stopped" and car.link and car.pi_state == "stopped":
            self._tcp(car, m.RESUME)    # 위치 지정으로 이미 arrived면 resume을 보내지 않음 (파이가 거절함)
        if new == "teach":
            car.recorder = Recorder()
        self._set_mode(car, new, "button")

    def _ui_record_end(self, car: Car, msg: Dict[str, Any], now: float) -> None:
        rec = car.recorder
        if rec is None or not rec.steps:
            return self._reject(car.n, "기록된 단계가 없음")
        try:
            task_id = self.store.save(msg["name"], car.n, car.aihand_name, rec.steps)
        except TaskError as e:
            return self._reject(car.n, str(e))
        self.log.event("task_saved", car=car.n, task=task_id, steps=len(rec.steps))
        car.recorder = None
        self._set_mode(car, "manual", "record_end")

    def _ui_goto(self, car: Car, msg: Dict[str, Any], now: float) -> None:
        if msg["station"] not in self.car_stations[car.n]:
            return self._reject(car.n, f"작업차 {car.n} 고리에 없는 정거장: {msg['station']}")
        self._request_goto(car, msg["station"], now, "live")

    def _ui_set_station(self, car: Car, msg: Dict[str, Any], now: float) -> None:
        if msg["station"] not in self.car_stations[car.n]:
            return self._reject(car.n, f"작업차 {car.n} 고리에 없는 정거장: {msg['station']}")
        car.pending_set_station = msg["station"]
        car.between = False
        self._tcp(car, m.SET_STATION, station=msg["station"])

    def _ui_start(self, car: Car, msg: Dict[str, Any], now: float) -> None:
        try:
            task = self.store.load(msg["task"])
        except TaskError as e:
            return self._reject(car.n, str(e))
        if task["car"] != car.n:
            return self._reject(car.n, f"작업차 {task['car']}에서 저장한 작업")
        if car.calib is None:
            return self._reject(car.n, f"작업차 {car.n} AiHand 보정 필요")
        if not self.vision_up:
            return self._reject(car.n, "맵 비전 서비스 꺼짐")
        plan = build_plan(task, self.mp.wait_station(car.n))
        for s in plan:
            if s["type"] == "goto" and s["station"] != STORAGE_ANY and s["station"] not in self.car_stations[car.n]:
                return self._reject(car.n, f"작업에 이 작업차 고리에 없는 정거장이 있음: {s['station']}")
        car.run = Run(msg["task"], task, plan, msg["repeat"])
        car.run.started_at = now
        car.repeat = msg["repeat"]
        car.cycle = 1
        car.result_pending, car.result_cycle = False, None
        car.auto_finished_at = None
        self._set_mode(car, "auto", "start")

    def _ui_mark_result(self, car: Car, msg: Dict[str, Any], now: float) -> None:
        if msg["cycle"] != car.result_cycle:
            return self._reject(car.n, f"결과를 표시할 반복이 아님 ({msg['cycle']})")
        self.log.event("result_mark", car=car.n, task=car.result_task, cycle=msg["cycle"], result=msg["result"])
        car.result_pending = False

    # ------------------------------------------------------------------ ui/state

    def _step_text(self, car: Car) -> Optional[str]:
        r = car.run
        if r is None:
            return None
        if r.phase == "wait_cond":
            return "출발 조건 기다림"
        return f"{r.idx + 1}/{len(r.plan)}"

    def ui_state(self) -> Dict[str, Any]:
        cars = {}
        for n, c in self.cars.items():
            cars[str(n)] = {
                "mode": c.mode, "link": c.link, "pi_state": c.pi_state, "station": c.station,
                "moving_to": c.moving_to if c.moving_to else (c.pending_goto[0] if c.pending_goto else None),
                "task": c.run.task["name"] if c.run else None, "step": self._step_text(c),
                "repeat": c.run.repeat if c.run else False, "cycle": c.cycle if c.mode == "auto" else 0,
                "recording_steps": len(c.recorder.steps) if c.recorder is not None else None,
                "result_pending": c.result_pending, "result_cycle": c.result_cycle,
                "video_url": c.video_url, "allowed": self.allowed(c),
            }
        fields = {
            "selected_car": self.selected, "cars": cars, "bench": self.bench,
            "bench_vial": self.bench_view.value, "slots": self.slots(), "cock_led": self.cock_led,
            "hand": {"service": self.hand_up, "detected": self.hand_detected, "calibrated": self.hand_calibrated},
            "vision": {"service": self.vision_up},
            "map_video_url": self.map_video_url, "hand_video_url": self.hand_video_url,
            "tasks": self.store.list(),
        }
        return self.out.make(m.UI_STATE, **fields)

    def calib_missing(self) -> List[int]:
        return [n for n, c in self.cars.items() if c.calib is None]
