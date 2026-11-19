# pi/ — 작업차의 라즈베리 파이 서비스

- **환경:** Raspbot 기본 이미지의 Raspberry Pi OS(리눅스), 이미지 기본 Python 3. 가상 환경은 `python3 -m venv --system-site-packages .venv`로 만든다. Yahboom 라이브러리(모터·선 센서)가 시스템 Python에만 있기 때문이다.
- 두 파이는 **같은 코드**를 쓰고 `pi/config/car.yaml`의 `car_id`(1 또는 2)만 다르다. 코드 안에서 작업차 번호에 따라 갈라지지 않게 한다.
- 개발은 노트북에서 하고 git으로 올린다. 파이에서는 `git pull` → `sudo systemctl restart proxyhand-car proxyhand-video`.

## 안전 규칙 (가장 먼저 지킨다)

- 관제의 생존 신호(`heartbeat`)가 1초 넘게 안 오면 **스스로 바퀴를 멈추고** `stopped`가 된다. 연결이 돌아와도 스스로 움직이지 않는다.
- 손 명령(`car{n}/hand`)은 상태가 `arrived`일 때만 micro:bit로 넘긴다. 다른 상태에서는 조용히 버린다.
- 손 명령은 **가장 최근 값만** 기억했다가 micro:bit가 `OK`를 보낼 때 보낸다. 쌓아 두지 않는다.
- 파이가 켜질 때 서보를 움직이지 않는다.
- 상태와 받는 명령 표는 `docs/05` 4.4, 선 따라가기·정지선 규칙은 `docs/04` 10장을 그대로 따른다.

## 숫자

- 속도·조향 값은 `car.yaml`에서 읽는다. 값이 0이면 시작할 때 오류를 낸다 (1차 시험 전이라는 뜻).
- 정지선 구간 시간은 `shared/config/map.yaml`의 `segment_s`. 0인 정거장이 있으면 정지선 놓침 확인을 끄고 경고만 출력한다.

## 시계

- 메시지의 `t`와 로그 시각에는 `time_sync`로 받은 차이값을 더한다. 시스템 시계는 바꾸지 않는다.

## 하드웨어 없이 시험

- 노트북에는 Yahboom 라이브러리와 micro:bit가 없다. 모터·선 센서·시리얼은 작은 클래스로 감싸 두고, 시험할 때는 가짜로 바꿔 끼울 수 있게 만든다.
- 관제 없이 시험할 때는 가짜 관제(TCP 5006에 접속해 `goto`·`heartbeat`를 보내는 작은 프로그램)를 쓴다.
- 로그: `~/proxyhand/logs/<날짜>.jsonl` (`docs/05` 9.8).
