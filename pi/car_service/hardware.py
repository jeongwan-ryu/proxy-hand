"""
작업차 하드웨어 감싸기 (파이에서만 동작). 시험에서는 tests/fakes/의 가짜로 바꿔 끼운다.

- RaspbotHW: 바퀴 모터(I2C 0x16, smbus로 직접) + 바닥 선 센서 4개(GPIO)
- SerialLink: micro:bit USB 시리얼 (인터페이스 정의 7장)

라이브러리(RPi.GPIO, smbus, pyserial)는 쓰는 순간에 불러온다. 노트북에서 import만 해도 깨지지 않게.
파이의 Python 버전에 맞추기 위해 3.9 문법까지만 쓴다.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from shared import messages as m

# 구형 Raspbot 선 센서 GPIO 핀 (BOARD 번호), 왼쪽부터 L2 L1 R1 R2.
# Yahboom 예제의 Tracking_Left1=13, Tracking_Left2=15, Tracking_Right1=11, Tracking_Right2=7.
# 확정 필요 (1차 시험): 선 위에 한 센서씩 올려 보고 순서가 맞는지 확인.
# Yahboom 센서는 검정에서 LOW(0)를 내므로 car.yaml의 invert_sensors: true가 맞을 가능성이 큼.
SENSOR_PINS = (13, 15, 11, 7)

# 모터 보드 (I2C 0x16). 파이에서 smbus로 직접 움직여 확인한 방식:
# 레지스터 1에 [왼쪽 방향, 왼쪽 속도, 오른쪽 방향, 오른쪽 속도] (방향 1 = 앞, 0 = 뒤), 레지스터 2에 [0] = 멈춤
I2C_BUS = 1
MOTOR_ADDR = 0x16
REG_RUN = 0x01
REG_STOP = 0x02
FORWARD = 1
MOTOR_MAX = 255     # 속도 값 범위 0~255


class RaspbotHW:
    def __init__(self) -> None:
        import RPi.GPIO as GPIO            # 파이 시스템 Python에 있음 (venv --system-site-packages)
        import smbus                       # 모터 보드 I2C (YB_Pcb_Car 라이브러리 없이 직접, 파이에서 확인함)
        self._gpio = GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)
        for pin in SENSOR_PINS:
            GPIO.setup(pin, GPIO.IN)
        self._bus = smbus.SMBus(I2C_BUS)
        self._last: Optional[Tuple[int, int]] = None
        self.stop()

    def read_sensors(self) -> Tuple[int, int, int, int]:
        """핀 값 그대로 (L2, L1, R1, R2). 검정=1로 맞추는 일은 invert_sensors로 작업차 서비스가 함."""
        return tuple(int(self._gpio.input(pin)) for pin in SENSOR_PINS)  # type: ignore[return-value]

    def drive(self, left: float, right: float) -> None:
        cmd = (max(0, min(MOTOR_MAX, int(left))), max(0, min(MOTOR_MAX, int(right))))
        if cmd == self._last:
            return                          # 같은 값을 I2C로 계속 보내지 않음
        self._last = cmd
        if cmd == (0, 0):
            self._bus.write_i2c_block_data(MOTOR_ADDR, REG_STOP, [0])
        else:
            self._bus.write_i2c_block_data(MOTOR_ADDR, REG_RUN, [FORWARD, cmd[0], FORWARD, cmd[1]])

    def stop(self) -> None:
        self._last = None
        self.drive(0, 0)


class SerialLink:
    """micro:bit와 명령 하나 → 응답 하나. 응답이 timeout 안에 없으면 None (실패 1번)."""

    def __init__(self, port: str, baud: int, timeout_s: float) -> None:
        self.port, self.baud, self.timeout_s = port, baud, timeout_s
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None

    def open(self) -> bool:
        try:
            import serial  # pyserial
            self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout_s)
            return True
        except Exception as e:  # noqa: BLE001 (포트 없음, 권한 등 모두 "연결 안 됨")
            print(f"[시리얼] {self.port} 열기 실패: {e}")
            self._ser = None
            return False

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
        self._ser = None

    def exchange(self, cmd: bytes) -> Optional[Tuple[bool, str]]:
        """명령을 보내고 OK/ERR 응답을 기다림. 확장보드 상태 글자(A...$) 같은 다른 줄은 건너뜀 (7장)."""
        if self._ser is None:
            return None
        try:
            self._ser.reset_input_buffer()   # 쌓여 있던 상태 정보를 버림 (기술조사 코드와 같은 방식)
            self._ser.write(cmd)
            deadline = time.monotonic() + self.timeout_s
            while time.monotonic() < deadline:
                line = self._ser.readline()
                if not line:
                    continue
                try:
                    return m.parse_serial_reply(line)
                except m.MessageError:
                    continue
            return None
        except Exception as e:  # noqa: BLE001 (USB가 빠짐 등)
            print(f"[시리얼] 오류: {e}")
            self.close()
            return None
