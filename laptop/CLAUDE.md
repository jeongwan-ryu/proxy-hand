# laptop/ — 노트북 서비스

- **환경:** Windows 11, Python 3.11 64비트, 저장소 맨 위의 `.venv` 하나. 패키지는 `laptop/requirements.txt`.
- 셸 명령과 경로는 Windows 기준이다. `start_laptop.bat`은 CRLF 줄바꿈을 유지한다.

## 서비스 사이 연결 (자세한 내용은 `docs/05`)

```
손 인식 ─UDP 5100 hand/pose─▶ 관제 ◀─UDP 5101 vision/state─ 맵 비전
        ◀─UDP 5102 hand/calib─  │
                                ├─ WebSocket :8000/ws ─ 브라우저 (조종 화면)
                                ├─ UDP 5005 car{n}/hand ─▶ 파이
                                └─ TCP 5006 명령·보고 ◀─▶ 파이 (관제가 접속)
맵 비전 ─ HTTP :8090/video ─▶ 브라우저 (맵 위 영상)
손 인식 ─ HTTP :8091/video ─▶ 브라우저 (손 영상)
```

- 포트·IP는 코드에 쓰지 않고 `shared/config/network.yaml`에서 읽는다.

## 웹캠

- 노트북에 웹캠이 2대(내장, 맵 위)라 번호가 바뀔 수 있다. 손 인식은 `laptop/config/laptop.yaml`의 `hand_camera_index`, 맵 비전은 `data/calibration/topcam.json`의 `camera_index`를 쓴다.
- 손 인식은 사진을 **좌우로 뒤집은 뒤** MediaPipe에 넣는다 (`mirror: true`). 왼손·오른손 구분이 거울 기준이기 때문이다.

## 관제 서비스

- 모든 메시지가 모이는 곳이다. 손 명령은 반드시 관제를 거쳐 파이로 간다 (손 명령 조건 검사, 작업차별 보정값, 기록을 한곳에서 하기 위해).
- 모드·작업대 상태 규칙은 `docs/04` 6장, 오류 처리는 11장, 화면 버튼 조건은 12장을 그대로 따른다.
- 조종 화면은 빌드 도구 없이 HTML + JavaScript 파일 그대로 쓴다 (`control_service/web/`).

## 하드웨어 없이 시험

- 파이가 없을 때는 가짜 작업차(TCP 5006을 열고 `status`·`arrived`를 보내는 작은 프로그램)를 상대로 시험한다.
- 웹캠이 없을 때는 저장해 둔 사진·영상 파일을 넣어 시험한다.
