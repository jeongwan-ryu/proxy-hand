"""
자세 변환: 손 모양(hand/pose) → AiHand 서보 각도 (시스템 설계 9.3, 9.5).

손 인식 서비스는 어느 작업차인지 모르고 손 모양만 보낸다. 작업차별 보정값(aihand_A/B.json)은 여기서 적용한다.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

MIN_CHANGE_DEG = 2      # 모든 서보 변화가 이보다 작으면 보내지 않음 (설계 9.3)


def _limits(lo: int, hi: int, margin: int) -> tuple:
    a, b = min(lo, hi) + margin, max(lo, hi) - margin
    return (a, b) if a <= b else ((lo + hi) // 2, (lo + hi) // 2)


def clamp_servo(i: int, angle: float, calib: Dict[str, Any]) -> int:
    """서보 i(0~5)의 실제로 쓰는 범위로 자름: 보정값에서 margin_deg 안쪽 (SR-03)."""
    s = calib["servos"][i]
    lo, hi = (s["open"], s["closed"]) if i < 5 else (s["min"], s["max"])
    a, b = _limits(lo, hi, calib["margin_deg"])
    return int(round(max(a, min(b, angle))))


def clamp_all(angles: Sequence[float], calib: Dict[str, Any]) -> List[int]:
    return [clamp_servo(i, a, calib) for i, a in enumerate(angles)]


def hand_to_servos(fingers: Sequence[float], wrist: float, calib: Dict[str, Any]) -> List[int]:
    """손가락: 편 각도 + r × (쥔 각도 − 편 각도). 손목: 가운데 각도 + wrist. 범위 밖은 자름."""
    out = []
    for i in range(5):
        s = calib["servos"][i]
        out.append(s["open"] + float(fingers[i]) * (s["closed"] - s["open"]))
    out.append(calib["servos"][5]["center"] + float(wrist))
    return clamp_all(out, calib)


def initial_pose(calib: Dict[str, Any]) -> List[int]:
    """처음 자세: 손가락 5개 = 편 각도, 손목 = drive_wrist (설계 9.5)."""
    return clamp_all([calib["servos"][i]["open"] for i in range(5)] + [calib["drive_wrist"]], calib)


def drive_pose(last: Optional[Sequence[int]], calib: Dict[str, Any]) -> List[int]:
    """주행 자세: 손목만 drive_wrist로. 손가락은 마지막으로 보낸 그대로 (바이알을 쥔 채 이동, 설계 9.5)."""
    base = list(last) if last is not None else initial_pose(calib)
    base[5] = calib["drive_wrist"]
    return clamp_all(base, calib)


def changed_enough(new: Sequence[int], last: Optional[Sequence[int]]) -> bool:
    if last is None:
        return True
    return any(abs(a - b) >= MIN_CHANGE_DEG for a, b in zip(new, last))
