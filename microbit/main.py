# =====================================================================
#  AiHand 브리지 (micro:bit V2 + MicroPython)
# ---------------------------------------------------------------------
#  역할: PC가 USB로 보낸 "텍스트 명령"을 받아서
#        Hiwonder 확장보드가 이해하는 "바이트 패킷"으로 바꿔 전달한다.
#
#  PC  --(USB, 텍스트 한 줄)-->  micro:bit  --(UART P12/P8, 10바이트)-->  확장보드 --> 서보
#
#  [지원 명령]  (한 줄 = 명령 하나, 끝에 줄바꿈 \n)
#    P                     : 연결 확인(ping)     -> "OK PONG"
#    S 서보번호 각도 시간ms : 서보 이동           -> "OK servo=1 angle=120 ms=500"
#        예) S 1 120 500
#    D                     : 초음파 거리         -> "OK cm=12.3 mm=123"
#    L 빨강 초록 파랑(0~255) : 초음파 센서 LED 색  -> "OK led=255,0,0"
#    I                     : I2C 장치 주소 검색   -> "OK i2c=0x77,..."
#    C                     : 카메라 인식 결과     -> "OK func=5 n=1 objs=1,160,120,40,38"
#                            (objs = 번호,x,y,폭,높이 ; 여러 개면 ';'로 구분, 최대 4개)
#    C F 기능번호           : 카메라 기능 전환     -> "OK func=5"  (1 얼굴, 5 색, 7 태그)
#    C L 0/1               : 카메라 보조등 끄기/켜기 -> "OK cam_led=1"
#    C R                   : 결과 영역 앞 64바이트 원본(16진수, 분석용) -> "OK func=5 raw=0001..."
#    그 외 / 잘못된 값      :                    -> "ERR 이유"
#
#  [초음파 센서] Hiwonder RGBSonar 확장 소스(RGBSonar.ts) 분석 결과
#    - 확장보드 UART를 거치지 않고 micro:bit I2C(P19=SCL, P20=SDA)에 직접 연결된다.
#      (확장보드의 I2C 포트는 P19/P20을 그대로 이어 주는 단자)
#    - I2C 주소 0x77
#    - 거리: 레지스터 0부터 2바이트(하위, 상위) = mm 단위. 5000(5m) 넘으면 측정 불가로 봄
#    - LED: 레지스터 2 = 모드(0 고정색, 1 숨쉬기), 레지스터 3~5 = LED1 RGB, 6~8 = LED2 RGB
#    - I2C는 UART와 별개라서 D/L/I/C 명령은 USB<->확장보드 전환이 필요 없다.
#
#  [WonderCam] Hiwonder WonderCam 확장 소스(custom.ts) 분석 결과
#    - micro:bit I2C에 직접 연결, 기본 주소 0x32
#    - 레지스터 주소가 2바이트(하위, 상위). 읽기 = 주소 2바이트 쓰고 -> 필요한 만큼 읽기
#    - 0x0000: 1바이트 'v'(0x76)면 카메라 준비 완료
#    - 0x0035: 현재 기능 번호(읽기/쓰기로 전환). 0x0030: 보조등 on/off
#    - 결과 영역(기능별 시작 주소): 얼굴 0x0400, 색 0x1000, AprilTag 0x1E00
#        [1] = 인식된 개수
#        색/태그: [2..] = 인식된 번호들, 얼굴: [4..] = 얼굴 번호들(0xFF = 학습 안 된 얼굴)
#        0x30 + k*크기 : k번째 결과의 x, y, 폭, 높이 (각 2바이트, 하위 먼저)
#        크기: 색/얼굴 16바이트, 태그 32바이트
#
#  [중요] "OK"는 "확장보드에 패킷을 보냈다"는 뜻이지 "서보가 도착했다"는 뜻이 아니다.
#         서보가 다 움직였는지는 PC 쪽에서 시간ms 만큼 기다려서 판단한다.
#
#  [UART 하나를 번갈아 쓰는 이유]
#    micro:bit의 UART(직렬 통신 장치)는 1개뿐이다.
#    평소엔 USB(PC)에 연결해 두고, 서보 명령을 보낼 때만 잠깐 P12/P8(확장보드)로 바꿨다가
#    다시 USB로 돌아온다. 바뀌어 있는 동안 PC가 보낸 글자는 사라지므로,
#    PC는 반드시 "OK"/"ERR" 응답을 받은 뒤에 다음 명령을 보내야 한다.
# =====================================================================

from microbit import uart, i2c, pin8, pin12, display, Image, sleep

BAUD = 115200          # 통신 속도(초당 비트 수). StartbitV2와 동일
SERVO_IDS = (1, 2, 3, 4, 5, 6)   # 이 손에서 쓰는 서보 번호
DEFAULT_MS = 500       # 기본 이동 시간(ms)
MIN_MS = 20            # 이동 시간 하한(너무 짧으면 서보에 충격)
MAX_MS = 10000         # 이동 시간 상한
MAX_LINE = 64          # 한 줄 최대 길이(쓰레기 데이터 방지)
SONAR_ADDR = 0x77      # 초음파 센서 I2C 주소


# ----------------------------- 초음파 센서 -----------------------------
def sonar_read_mm():
    """거리(mm) 읽기: 레지스터 0을 지정한 뒤 2바이트 읽기(하위 바이트 먼저)"""
    i2c.write(SONAR_ADDR, bytes([0]))
    data = i2c.read(SONAR_ADDR, 2)
    return data[0] + data[1] * 256


def sonar_set_led(r, g, b):
    """두 LED를 같은 색으로(고정색 모드)"""
    i2c.write(SONAR_ADDR, bytes([2, 0]))                 # 레지스터2 = 0(고정색 모드)
    i2c.write(SONAR_ADDR, bytes([3, r, g, b, r, g, b]))  # 레지스터3부터 LED1, LED2 RGB


# ----------------------------- WonderCam -----------------------------
CAM_ADDR = 0x32
CAM_FUNC_REG = 0x0035
CAM_LED_REG = 0x0030
CAM_MAX_OBJS = 4                       # 응답에 담을 최대 개수
# 기능 번호: (결과 시작 주소, 결과 하나의 크기)
CAM_RESULT = {1: (0x0400, 16),         # 얼굴 인식
              5: (0x1000, 16),         # 색 인식
              7: (0x1E00, 32)}         # AprilTag(태그)


def cam_read(reg, n):
    i2c.write(CAM_ADDR, bytes([reg & 0xFF, (reg >> 8) & 0xFF]))
    return i2c.read(CAM_ADDR, n)


def cam_write(reg, value):
    i2c.write(CAM_ADDR, bytes([reg & 0xFF, (reg >> 8) & 0xFF, value & 0xFF]))


def s16(buf, i):
    """2바이트(하위 먼저)를 부호 있는 정수로"""
    v = buf[i] | (buf[i + 1] << 8)
    return v - 65536 if v >= 32768 else v


def cam_ready():
    return cam_read(0x0000, 1)[0] == ord("v")


def cam_results():
    func = cam_read(CAM_FUNC_REG, 1)[0]
    if func not in CAM_RESULT:
        return "OK func={} n=0 objs=-".format(func)      # 지원하지 않는 기능
    base, size = CAM_RESULT[func]
    buf = cam_read(base, 0x30 + size * CAM_MAX_OBJS)     # 앞부분만 읽음(빠르게)
    n = buf[1]
    objs = []
    for k in range(min(n, CAM_MAX_OBJS)):
        if func == 1:                                    # 얼굴: 번호는 4번째 바이트부터
            oid = buf[4 + k]
            if oid == 0xFF:
                oid = 0                                  # 학습 안 된 얼굴 = 0
        else:                                            # 색/태그: 번호는 2번째 바이트부터
            oid = buf[2 + k]
        off = 0x30 + k * size
        objs.append("{},{},{},{},{}".format(
            oid, s16(buf, off), s16(buf, off + 2), s16(buf, off + 4), s16(buf, off + 6)))
    return "OK func={} n={} objs={}".format(func, n, ";".join(objs) if objs else "-")


def cam_set_function(func):
    """기능 전환 후 실제로 바뀔 때까지 최대 4초 기다림(MakeCode 소스와 같은 방식)
    이미 그 기능이면 아무것도 쓰지 않는다(카메라가 불필요하게 다시 로딩하지 않도록)."""
    if cam_read(CAM_FUNC_REG, 1)[0] == func:
        return True
    cam_write(CAM_FUNC_REG, func)
    for _ in range(40):
        sleep(100)
        if cam_read(CAM_FUNC_REG, 1)[0] == func:
            return True
    return False


def handle_cam(parts):
    try:
        if not cam_ready():
            return "ERR cam not ready"
        if len(parts) == 1:
            return cam_results()
        sub = parts[1].upper()
        if sub == "F" and len(parts) == 3:
            func = int(parts[2])
            if func not in CAM_RESULT:
                return "ERR func must be 1(face) 5(color) 7(tag)"
            if cam_set_function(func):
                return "OK func={}".format(func)
            # 카메라가 메뉴/학습 화면에 있으면 전환 명령을 무시할 수 있다 -> 현재 기능 번호를 알려 줌
            return "ERR func switch timeout (now={})".format(cam_read(CAM_FUNC_REG, 1)[0])
        if sub == "R":                    # 원본 바이트 보기(분석용): 앞 64바이트를 16진수로
            func = cam_read(CAM_FUNC_REG, 1)[0]
            if func not in CAM_RESULT:
                return "OK func={} raw=-".format(func)
            buf = cam_read(CAM_RESULT[func][0], 64)
            return "OK func={} raw={}".format(func, "".join("%02x" % b for b in buf))
        if sub == "L" and len(parts) == 3:
            on = 1 if parts[2] == "1" else 0
            cam_write(CAM_LED_REG, on)
            return "OK cam_led={}".format(on)
        return "ERR usage: C | C F n | C L 0/1 | C R"
    except OSError:
        return "ERR cam not found (I2C 0x32)"
    except ValueError:
        return "ERR not a number"


def angle_to_position(angle):
    """각도(0~180)를 확장보드 위치값(500~2500)으로 변환. 90도 -> 1500"""
    return int(500 + angle * 2000 / 180 + 0.5)


def make_servo_packet(servo, angle, ms):
    """StartbitV2.ts의 setPwmServo()와 똑같은 10바이트 패킷을 만든다.
    0x55 0x55 | 0x08(길이) | 0x03(서보 명령) | 0x01(서보 개수)
    | 시간 하위 | 시간 상위 | 서보번호 | 위치 하위 | 위치 상위
    """
    pos = angle_to_position(angle)
    return bytes([
        0x55, 0x55, 0x08, 0x03, 0x01,
        ms & 0xFF, (ms >> 8) & 0xFF,
        servo,
        pos & 0xFF, (pos >> 8) & 0xFF,
    ])


def send_to_board(packet):
    """UART를 확장보드 쪽(P12=TX, P8=RX)으로 잠깐 돌려서 패킷을 보내고 USB로 복귀.
    주의: 이 함수 안에서는 print()를 쓰면 안 된다(글자가 확장보드로 가버림)."""
    uart.init(baudrate=BAUD, tx=pin12, rx=pin8)
    sleep(2)                 # 핀 전환 안정화
    uart.write(packet)
    sleep(5)                 # 10바이트 전송 완료 대기(115200bps에서 약 1ms, 여유를 둠)
    uart.init(baudrate=BAUD)  # 핀 지정 없이 init -> USB로 복귀
    # [중요] 확장보드는 상태 정보(전압 등, "A...$")를 P8로 주기적으로 보낸다.
    # 위에서 P8에 연결돼 있던 동안 들어온 글자가 버퍼에 남으면 다음 PC 명령 앞에 붙어서
    # 'unknown command'가 된다. PC는 OK를 받기 전엔 아무것도 안 보내므로 지금 버퍼에
    # 있는 건 전부 확장보드에서 온 글자 -> 모두 버린다.
    sleep(2)
    while uart.any():
        uart.read()


def reply(text):
    """PC로 응답 한 줄 보내기 (반드시 USB 상태에서 호출)"""
    uart.write(text + "\n")


def move_servo(servo, angle, ms):
    """값 검사 + 제한 후 서보 이동. 실제로 보낸 값을 돌려준다."""
    angle = max(0, min(180, angle))     # 각도 0~180으로 제한(clamp)
    ms = max(MIN_MS, min(MAX_MS, ms))   # 시간 제한
    display.show(str(servo), wait=False)  # LED에 서보 번호 표시
    send_to_board(make_servo_packet(servo, angle, ms))
    return angle, ms


def handle_line(line):
    """텍스트 명령 한 줄을 해석하고 응답 문자열을 돌려준다."""
    parts = line.split()
    if not parts:
        return None                     # 빈 줄은 무시
    cmd = parts[0].upper()

    if cmd == "P":
        return "OK PONG"

    if cmd == "S":
        if len(parts) not in (3, 4):
            return "ERR usage: S servo angle [ms]"
        try:
            servo = int(parts[1])
            angle = int(parts[2])
            ms = int(parts[3]) if len(parts) == 4 else DEFAULT_MS
        except ValueError:
            return "ERR not a number"
        if servo not in SERVO_IDS:
            return "ERR servo must be 1-6"
        angle, ms = move_servo(servo, angle, ms)
        return "OK servo={} angle={} ms={}".format(servo, angle, ms)

    if cmd == "D":
        try:
            mm = sonar_read_mm()
        except OSError:
            return "ERR sonar not found (I2C 0x77)"
        return "OK cm={} mm={}".format(mm / 10, mm)

    if cmd == "L":
        if len(parts) != 4:
            return "ERR usage: L r g b"
        try:
            rgb = [max(0, min(255, int(p))) for p in parts[1:4]]
        except ValueError:
            return "ERR not a number"
        try:
            sonar_set_led(*rgb)
        except OSError:
            return "ERR sonar not found (I2C 0x77)"
        return "OK led={},{},{}".format(*rgb)

    if cmd == "C":
        return handle_cam(parts)

    if cmd == "I":
        found = i2c.scan()
        return "OK i2c=" + (",".join("0x%02x" % a for a in found) if found else "none")

    # 실제로 받은 글자를 보여줘서 원인 파악을 돕는다(너무 길면 앞 20자만)
    return "ERR unknown command: " + repr(line[:20])


# ----------------------------- 시작 동작 -----------------------------
uart.init(baudrate=BAUD)
display.show(Image.HEART)
sleep(2000)   # StartbitV2도 초기화 후 2초 대기. 확장보드가 켜질 시간을 준다.

# 시작 시 6개 서보를 90도(반쯤 쥔 기본 자세)로. 안전을 위해 한 번에 하나씩.
for sid in SERVO_IDS:
    move_servo(sid, 90, DEFAULT_MS)
    sleep(DEFAULT_MS + 100)

display.show(Image.YES)
reply("READY")

# ----------------------------- 메인 루프 -----------------------------
buf = ""
while True:
    if uart.any():
        data = uart.read()
        if data:
            # 일반 글자(ASCII)와 줄바꿈만 남기고 나머지 바이트는 버린다
            for b in data:
                if 32 <= b < 127 or b == 10 or b == 13:
                    buf += chr(b)
        # 줄바꿈(\n 또는 \r) 단위로 명령을 하나씩 처리
        while True:
            idx = -1
            for i, ch in enumerate(buf):
                if ch == "\n" or ch == "\r":
                    idx = i
                    break
            if idx < 0:
                break
            line = buf[:idx].strip()
            buf = buf[idx + 1:]
            answer = handle_line(line)
            if answer is not None:
                reply(answer)
        if len(buf) > MAX_LINE:     # 줄바꿈 없이 너무 길면 버림
            buf = ""
            reply("ERR line too long")
    else:
        sleep(1)
