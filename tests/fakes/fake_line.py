"""가짜 선 센서·모터 (RaspbotHW 대신). 정해 둔 센서 값을 차례로 돌려주고, 바퀴 값을 기록한다.

센서 값은 검정=1 기준 (L2, L1, R1, R2). invert_sensors: true로 시험할 때는 FakeLine(..., raw_black=0).
"""

from typing import Iterable, List, Tuple

CENTER = (0, 1, 1, 0)
STOPLINE = (1, 1, 1, 1)
NOTHING = (0, 0, 0, 0)


def route(*parts: Tuple[Tuple[int, int, int, int], int]) -> List[Tuple[int, int, int, int]]:
    """route((STOPLINE, 3), (CENTER, 10)) → 센서 값 목록."""
    out = []
    for value, count in parts:
        out += [value] * count
    return out


class FakeLine:
    def __init__(self, readings: Iterable[Tuple[int, int, int, int]] = (), raw_black: int = 1) -> None:
        self.readings = list(readings)
        self.raw_black = raw_black
        self.drives: List[Tuple[float, float]] = []
        self.stopped = 0

    def read_sensors(self) -> Tuple[int, int, int, int]:
        value = self.readings.pop(0) if len(self.readings) > 1 else (self.readings[0] if self.readings else CENTER)
        if self.raw_black == 0:
            value = tuple(1 - v for v in value)
        return value  # type: ignore[return-value]

    def drive(self, left: float, right: float) -> None:
        self.drives.append((left, right))

    def stop(self) -> None:
        self.stopped += 1
