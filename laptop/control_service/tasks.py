"""
가르치기 기록과 작업 파일 (시스템 설계 8장, 인터페이스 정의 9.7).

작업 하나 = data/tasks/<저장 시각>_<이름>.json. 파일 이름이 ui/state의 작업 id.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
from typing import Any, Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
TASKS_DIR = ROOT / "data" / "tasks"
STORAGE_ANY = "storage_*"           # "바이알이 있는 칸" (설계 5.1, SR-20)
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


class TaskError(ValueError):
    pass


def is_storage(station: str) -> bool:
    return station.startswith("storage_")


def safe_name(name: str) -> str:
    """파일 이름에 쓸 수 있게: 공백·금지 문자 → _ (설계 12장: 작업 이름에는 공백 대신 _)."""
    out = _BAD_CHARS.sub("_", name.strip()).strip("_.")
    return out[:40]


class Recorder:
    """가르치기 중 관제가 보낸 goto와 hand를 단계로 쌓는다 (설계 8장).
    도착 뒤 다음 goto까지 보낸 hand가 hand 단계 하나. 보관함 정거장은 storage_*로 저장."""

    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []
        self._last_t: Optional[float] = None

    def goto(self, station: str) -> None:
        self.steps.append({"type": "goto", "station": STORAGE_ANY if is_storage(station) else station})
        self._last_t = None

    def hand(self, angles: List[int], t: float) -> None:
        if not self.steps or self.steps[-1]["type"] != "hand":
            self.steps.append({"type": "hand", "frames": []})
            self._last_t = None
        dt = 0.0 if self._last_t is None else round(t - self._last_t, 3)
        self.steps[-1]["frames"].append({"dt": dt, "angles": list(angles)})
        self._last_t = t


class TaskStore:
    def __init__(self, folder: pathlib.Path = TASKS_DIR) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self._index: List[Dict[str, Any]] = []
        self.refresh()

    def refresh(self) -> None:
        index = []
        for path in sorted(self.folder.glob("*.json")):
            try:
                t = self._read(path)
            except TaskError as e:
                print(f"[작업 파일 무시] {e}")
                continue
            index.append({"id": path.name, "name": t["name"], "car": t["car"], "created": t["created"]})
        self._index = index

    def list(self) -> List[Dict[str, Any]]:
        return list(self._index)

    def _path(self, task_id: str) -> pathlib.Path:
        if "/" in task_id or "\\" in task_id or not task_id.endswith(".json"):
            raise TaskError(f"작업 id가 틀림: {task_id}")
        return self.folder / task_id

    def _read(self, path: pathlib.Path) -> Dict[str, Any]:
        try:
            t = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise TaskError(f"{path.name}: 읽을 수 없음 ({e})") from None
        if not isinstance(t, dict) or not all(k in t for k in ("name", "car", "created", "steps")):
            raise TaskError(f"{path.name}: 필드가 빠짐 (name, car, created, steps)")
        if t["car"] not in (1, 2) or not isinstance(t["steps"], list):
            raise TaskError(f"{path.name}: car 또는 steps가 틀림")
        for i, s in enumerate(t["steps"]):
            if s.get("type") == "goto" and isinstance(s.get("station"), str):
                continue
            if s.get("type") == "hand" and isinstance(s.get("frames"), list) and s["frames"] and all(
                    isinstance(f.get("angles"), list) and len(f["angles"]) == 6 and isinstance(f.get("dt"), (int, float))
                    for f in s["frames"]):
                continue
            raise TaskError(f"{path.name}: steps[{i}] 형식이 틀림")
        return t

    def load(self, task_id: str) -> Dict[str, Any]:
        path = self._path(task_id)
        if not path.exists():
            raise TaskError(f"작업이 없음: {task_id}")
        return self._read(path)

    def save(self, name: str, car: int, hand_calibration: str, steps: List[Dict[str, Any]],
             now: Optional[datetime.datetime] = None) -> str:
        now = now or datetime.datetime.now()
        clean = safe_name(name)
        if not clean:
            raise TaskError("작업 이름이 비어 있음")
        task_id = f"{now:%Y%m%d_%H%M%S}_{clean}.json"
        data = {"name": name.strip(), "car": car, "created": now.isoformat(timespec="seconds"),
                "hand_calibration": hand_calibration, "steps": steps}
        path = self._path(task_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        self.refresh()
        return task_id

    def delete(self, task_id: str) -> None:
        path = self._path(task_id)
        if not path.exists():
            raise TaskError(f"작업이 없음: {task_id}")
        path.unlink()
        self.refresh()


def build_plan(task: Dict[str, Any], wait_station: str) -> List[Dict[str, Any]]:
    """재생할 단계 목록. 마지막 goto가 대기 정거장이 아니면 대기 정거장 이동을 붙임 (설계 8장). 파일은 그대로."""
    plan = [dict(s) for s in task["steps"]]
    gotos = [s for s in plan if s["type"] == "goto"]
    if not gotos or gotos[-1]["station"] != wait_station:
        plan.append({"type": "goto", "station": wait_station, "auto_added": True})
    return plan


def uses_storage(plan: List[Dict[str, Any]]) -> bool:
    return any(s["type"] == "goto" and s["station"] == STORAGE_ANY for s in plan)
