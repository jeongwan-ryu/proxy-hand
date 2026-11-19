# ProxyHand

AiHand(로봇 손) + Raspbot 작업차 2대로 핫셀 원격 조종 · 가르치기 · 혼자 하기를 시연하는 프로젝트. 팀원 2명: 이보연, 유정완.

## 기준 문서 (`docs/`)

| 파일 | 내용 | 언제 읽나 |
| --- | --- | --- |
| `docs/01_기획서.md` | 왜 만드는지, 시연 시나리오, 맵 | 애매한 판단의 근거가 필요할 때 |
| `docs/02_사용자_요구사항.md` | 사용자가 할 수 있어야 하는 것 (UR) | 기능 범위를 확인할 때 |
| `docs/03_시스템_요구사항.md` | 시스템이 해야 하는 것 (SR) | 구현·시험 기준 |
| `docs/04_시스템_설계.md` | 서비스 구성, 동작 규칙, 오류 처리 | 서비스를 구현할 때 |
| `docs/05_인터페이스_정의.md` | 메시지, 오류 코드, 설정·데이터 파일 형식 | 서비스 사이에 주고받는 것을 다룰 때 **항상** |

- **문서가 코드보다 우선.** 코드와 인터페이스 정의가 다르면 문서가 맞다고 보고 코드를 고친다.
- 메시지·오류 코드·설정 필드를 바꾸거나 더하려면 `docs/05`를 먼저 고치고, 코드와 **같은 커밋**에 넣는다. 다른 팀원 코드에 영향이 있으므로 커밋 메시지에 `[interface]`를 붙인다.
- 문서 안의 "(잠정)" 숫자는 1차 시험 후 바뀐다. 코드에 직접 박지 말고 설정 파일에서 읽는다.
- Confluence는 확정본을 옮겨 두는 사본이다. 문서는 여기서만 고친다.

## 폴더와 담당

| 폴더 | 서비스 | 실행 기계 | 담당 |
| --- | --- | --- | --- |
| `laptop/hand_service/` | 손 인식 | 노트북 | 이보연 |
| `laptop/control_service/` (+ `web/`) | 관제 + 조종 화면 | 노트북 | 이보연 |
| `laptop/vision_service/` | 맵 비전 | 노트북 | 유정완 |
| `laptop/tools/aihand_calib.py` | AiHand 보정 | 노트북 | 이보연 |
| `laptop/tools/topcam_calib.py` | 맵 위 웹캠 보정 | 노트북 | 유정완 |
| `pi/car_service/`, `pi/video_service/` | 작업차, 영상 | 파이 2대 | 유정완 |
| `microbit/` | micro:bit 손 제어 | micro:bit | 이보연 |
| `shared/` | 메시지·설정 공통 코드 | 노트북·파이 | 같이 |

폴더마다 그 환경의 규칙을 적은 `CLAUDE.md`가 따로 있다.

## 구조 규칙

- **서비스 하나 = 따로 실행되는 프로그램 하나.** 서비스끼리 서로 import하지 않는다. 같이 쓰는 코드는 `shared/`에만 둔다.
- 메시지 이름과 필드는 `shared/messages.py`의 상수·함수로만 만든다. 문자열 `"car1/hand"` 같은 것을 서비스 코드에 직접 쓰지 않는다.
- 설정·보정 파일은 `shared/config.py`로만 읽는다. 서비스가 YAML·JSON을 직접 열지 않는다.
- 설정이 빠졌거나 틀리면 시작할 때 바로 오류를 내고 멈춘다. 보정 파일이 없을 때만 예외로, 그 기능만 막고 뜬다 (`docs/05` 9.9).
- 다른 서비스가 꺼져 있어도 각 서비스는 떠야 한다.

## 실행

모든 서비스는 **저장소 맨 위 폴더에서** 모듈로 실행한다. 서비스 폴더 안에서 `python main.py`로 실행하면 `shared/`를 찾지 못한다.

```
python -m laptop.hand_service.main
python -m pi.car_service.main
```

- 노트북: `python -m venv .venv` → `.venv\Scripts\pip install -r laptop\requirements.txt` → `laptop\start_laptop.bat`
- 시험: 저장소 맨 위에서 `python -m pytest`. 코드를 고친 뒤에는 반드시 돌리고, 통과해야 끝난 것이다.
  - `tests/test_interface_doc.py`는 `docs/05`와 `shared/messages.py`가 같은지 본다. 여기서 실패하면 문서와 코드 중 하나가 바뀐 것이다. 시험을 고쳐서 통과시키지 말고, 어느 쪽이 맞는지 확인해서 문서나 코드를 고친다.
  - 하드웨어 없이 시험할 가짜들은 `tests/fakes/`에 둔다 (목록과 담당은 그 폴더의 `__init__.py`).
- `shared/config/` 폴더에는 `__init__.py`를 넣지 않는다. 넣으면 `import shared.config`가 `config.py` 대신 폴더를 가리킨다.

## 하지 말 것

- Docker, ROS 2, DB 도입 (설계에서 쓰지 않기로 정함, `docs/04` 1장)
- 파이의 시스템 시계 바꾸기 (`time_sync` 차이값만 더해서 쓴다)
- `data/calibration/*.json`을 손으로 덮어쓰기 (보정 도구가 만든다)
- 상대 담당 폴더를 고칠 때 알리지 않기. 고쳤으면 커밋 메시지에 `[유정완 영역]`처럼 표시한다.

## 커밋

- 한 커밋에는 한 가지 일만.
- `data/logs/`, `.venv/`는 올리지 않는다 (`.gitignore`).
