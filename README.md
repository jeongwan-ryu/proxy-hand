# ProxyHand

AiHand + Raspbot 작업차 2대로 핫셀 원격 조종 · 가르치기 · 혼자 하기를 시연하는 프로젝트입니다.

기준 문서는 [`docs/`](docs/)에 있습니다. 문서는 여기서만 고치고, Confluence에는 확정본을 옮겨 둡니다.
- `01_기획서` → `02_사용자_요구사항` → `03_시스템_요구사항` → `04_시스템_설계` → `05_인터페이스_정의`
- 코드와 인터페이스 정의가 다르면 문서가 기준입니다.
- Claude Code용 규칙은 [`CLAUDE.md`](CLAUDE.md)와 폴더별 `CLAUDE.md`에 있습니다.

## 폴더 구조

```
docs/                      기준 문서 5개 + 그림 (images/)
laptop/                    노트북 (Windows, Python 3.11)
  hand_service/            손 인식 서비스        — 이보연
  vision_service/          맵 비전 서비스        — 유정완
  control_service/         관제 서비스           — 이보연
    web/                   조종 화면 (HTML, JS, CSS)
  tools/                   보정 도구 (aihand_calib.py, topcam_calib.py)
  config/laptop.yaml       노트북에만 있는 설정
  requirements.txt
  start_laptop.bat         노트북 서비스 3개를 한 번에 실행
pi/                        각 작업차의 라즈베리 파이
  car_service/             작업차 서비스         — 유정완
  video_service/           영상 서비스           — 유정완
  config/car.yaml          작업차마다 다른 설정 (car_id 1 또는 2)
  requirements.txt
  systemd/                 부팅 때 자동 실행 설정
microbit/                  micro:bit 손 제어 (MicroPython) — 이보연
shared/                    노트북·파이 공통
  config/network.yaml      IP, 포트, 시간 설정
  config/map.yaml          정거장, 마커, 판단 기준값
  config.py                설정 읽기 + 검사
  messages.py              메시지 만들기·보내기·받기
data/                      노트북에만 쓰는 파일 저장소
  calibration/             aihand_A.json, aihand_B.json, topcam.json, operator.json (도구가 만듦)
  tasks/                   저장한 작업
  logs/                    실행마다 폴더 하나 (git에 올리지 않음)
```

서비스 하나 = 따로 실행되는 Python 프로그램 하나입니다. 서비스끼리는 인터페이스 정의의 메시지로만 주고받습니다.
모든 서비스는 **저장소 맨 위 폴더에서** `python -m <폴더>.<서비스>.main` 으로 실행합니다(`shared`를 불러오기 위해).

## 노트북에서 실행

```bat
python -m venv .venv
.venv\Scripts\pip install -r laptop\requirements.txt
laptop\start_laptop.bat
```

브라우저로 http://localhost:8000 에 접속합니다.

## 시험

```bat
.venv\Scripts\python -m pytest
```

`tests/test_interface_doc.py`는 `docs/05_인터페이스_정의.md`와 `shared/messages.py`가 같은지 확인합니다.

## 파이에서 실행

```bash
git clone <저장소 주소> ~/proxy-hand && cd ~/proxy-hand
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r pi/requirements.txt
# pi/config/car.yaml 의 car_id 를 1 또는 2로 고침
sudo cp pi/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proxyhand-car proxyhand-video
```

코드를 고친 뒤: `git pull` → `sudo systemctl restart proxyhand-car proxyhand-video`

## 아직 정할 값

설정 파일에서 `확정 필요`라고 적힌 값입니다.
- `shared/config/network.yaml`: IP (공유기 설정 후)
- `shared/config/map.yaml`: 정거장별 `segment_s` (1차 시험 후)
- `pi/config/car.yaml`: 속도·조향 값 (1차 시험 후)
- `pi/systemd/*.service`: 파이 사용자 이름, 저장소 위치
