"""
손 따라 하기 시작과 이어 붙이기 (시스템 설계 9.6, SR-44).

    잠김 ──손을 쫙 폄(open_hold_s 유지)──▶ 시작 (이 편 손은 AiHand에 보내지 않음)
    시작 뒤: 손가락마다 조종자 손가락이 AiHand 손가락만큼 쥐어지면(catch_tol 안쪽) 그때부터 따라 함
             아직 안 붙은 손가락은 AiHand 각도를 그대로 둠. 손목은 시작 뒤 바로 따라 함.

AiHand가 바이알을 쥐고 있을 때 시작 신호(편 손) 직후의 "반쯤 쥔 손"을 따라 하면 바이알이 떨어지므로
이어 붙이기가 필요하다. 다시 잠그는 때는 관제(core.py)가 정한다.

놓기 경고 (시스템 설계 9.7, SR-45): AiHand가 쥐고 있고(손가락 평균 ≥ hold_r) 놓는 곳이 아닌데 손을 펴면
손가락을 멈추고(release_hold) [놓기 허용]을 기다림. 허용은 AiHand가 한 번 펴졌다 다시 쥐면 풀림.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

N_FINGERS = 5


def fingers_r(angles: Optional[Sequence[int]], calib: Optional[Dict[str, Any]]) -> Optional[List[float]]:
    """AiHand 서보 각도 → 손가락 쥔 정도 r(0 = 편 각도, 1 = 쥔 각도). 각도를 모르면 None."""
    if angles is None or calib is None:
        return None
    out = []
    for i in range(N_FINGERS):
        s = calib["servos"][i]
        span = s["closed"] - s["open"]
        r = 0.0 if span == 0 else (angles[i] - s["open"]) / span
        out.append(round(max(0.0, min(1.0, r)), 2))
    return out


class HandGate:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.open_r = cfg["open_r"]
        self.open_hold_s = cfg["open_hold_s"]
        self.catch_tol = cfg["catch_tol"]
        self.hold_r = cfg["hold_r"]
        self.release_hold = False       # 놓기 막음: 손가락 멈춤, [놓기 허용] 기다림
        self.release_allowed = False    # [놓기 허용]을 누름: 이번 한 번 펴는 것을 따라 함
        self.release_opened = False     # 허용 뒤 AiHand가 실제로 펴짐 (다시 쥐면 허용을 풀기 위해)
        self.locked = True
        self.open_since: Optional[float] = None
        self.attached = [False] * N_FINGERS

    def lock(self) -> bool:
        """잠금. 바뀌었으면 True."""
        changed = not self.locked or any(self.attached) or self.release_hold
        self.locked = True
        self.open_since = None
        self.attached = [False] * N_FINGERS
        self.release_hold = self.release_allowed = self.release_opened = False
        return changed

    def watch_start(self, fingers: Sequence[float], now: float) -> bool:
        """잠겨 있을 때 손 모양을 봄. 손가락 5개가 모두 open_r 이하로 open_hold_s 동안 이어지면 시작 → True."""
        if not self.locked:
            return False
        if all(r <= self.open_r for r in fingers):
            if self.open_since is None:
                self.open_since = now
            if now - self.open_since >= self.open_hold_s - 1e-6:     # 1e-6: 소수 계산 오차
                self.locked = False
                self.open_since = None
                self.attached = [False] * N_FINGERS
                return True
        else:
            self.open_since = None
        return False

    def reset_start_watch(self) -> None:
        """손이 안 보이는 등 이어서 보지 못했을 때: 편 손 유지 시간을 처음부터."""
        self.open_since = None

    def attach(self, fingers: Sequence[float], aihand: Optional[Sequence[float]]) -> bool:
        """시작 뒤: 붙을 손가락을 붙임. 바뀌었으면 True.
        AiHand 모양을 모르면(관제를 다시 켠 뒤 아직 보낸 적 없음) 바이알을 쥐고 있다고 보고 r = 1.0으로 비교함
        → 주먹을 쥐어야 붙음."""
        target = list(aihand) if aihand is not None else [1.0] * N_FINGERS
        changed = False
        for i in range(N_FINGERS):
            if self.attached[i]:
                continue
            if fingers[i] >= target[i] - self.catch_tol:
                self.attached[i] = True
                changed = True
        return changed

    def all_attached(self) -> bool:
        return all(self.attached)

    def holding(self, r: Optional[Sequence[float]]) -> bool:
        return r is not None and sum(r) / len(r) >= self.hold_r

    def guard_release(self, angles: List[int], last: Optional[Sequence[int]],
                      new_r: Sequence[float], last_r: Optional[Sequence[float]], release_ok: bool) -> str:
        """놓기 경고 (9.7). angles를 고칠 수 있음(손가락을 last로). 돌려주는 값:
        "" 변화 없음 / "hold" 막기 시작 / "clear" 막기 풀림(다시 쥠) / "reset" 허용이 풀림(다시 쥠)."""
        event = ""
        if self.release_allowed:
            if not self.holding(new_r):
                self.release_opened = True
            elif self.release_opened:
                self.release_allowed = self.release_opened = False
                event = "reset"
            return event
        if release_ok or not self.holding(last_r) or self.holding(new_r) or last is None:
            if self.release_hold and (release_ok or self.holding(new_r)):
                self.release_hold = False
                return "clear"
            if not self.release_hold:
                return ""
        # 쥐고 있는데 놓는 곳이 아닌 곳에서 펴려 함 → 손가락은 마지막 각도 그대로, 손목은 따라 감
        for i in range(N_FINGERS):
            angles[i] = last[i]  # type: ignore[index]
        if not self.release_hold:
            self.release_hold = True
            return "hold"
        return ""

    def allow_release(self) -> None:
        self.release_hold = False
        self.release_allowed = True
        self.release_opened = False

    def merge(self, wanted: Sequence[int], last: Optional[Sequence[int]]) -> List[int]:
        """보낼 각도: 붙은 손가락과 손목은 손을 따르고, 안 붙은 손가락은 마지막 AiHand 각도 그대로."""
        if last is None:
            return list(wanted)
        out = list(wanted)
        for i in range(N_FINGERS):
            if not self.attached[i]:
                out[i] = last[i]
        return out
