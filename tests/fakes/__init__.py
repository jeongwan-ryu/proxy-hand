"""
하드웨어·상대 서비스 없이 시험하기 위한 가짜들.

목록과 담당 (구현하면서 필요한 사람이 만든다):
- fake_car.py      가짜 작업차 2대: 진짜 작업차 서비스 + 가짜 선로(+ 가짜/진짜 micro:bit) — 이보연 (관제 시험용)
                   python -m tests.fakes.fake_car  (포트는 network_local.yaml, 콘솔로 오류 일으키기)
- network_local.yaml  작업차 2대를 127.0.0.1에 둔 네트워크 설정 (관제 --network로 씀)    — 이보연
- fake_control.py  가짜 관제: TCP 5006에 접속해 goto·heartbeat를 보냄            — 유정완 (작업차 서비스 시험용)
- fake_serial.py   가짜 micro:bit: P, M에 OK로 답함. 응답 없음·ERR도 흉내         — 유정완
- fake_line.py     가짜 선 센서·모터: 정해 둔 센서 값 순서를 돌려줌             — 유정완

가짜도 메시지는 반드시 shared/messages.py로 만든다. 그래야 가짜와 진짜가 같은 약속을 지킨다.
"""
