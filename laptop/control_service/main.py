"""
관제 서비스 (노트북) — 담당: 이보연

하는 일: 진행 관리(작업차 모드, 작업대 상태, 혼자 하기), 자세 변환(손 → 서보 각도),
        가르치기 기록·재생, 웹 서버(FastAPI, :8000, WebSocket /ws), 로그.
입력: hand/pose, vision/state, 파이 보고(TCP 5006), ui/cmd, data/ 파일
출력: car{n}/hand (UDP 5005), 파이 명령(TCP 5006), ui/state, ui/alert, hand/calib, data/ 파일
관련 SR: SR-02~04, SR-07, SR-08, SR-18~22, SR-24, SR-27~40
참고: 시스템 설계 6~9장, 인터페이스 정의 4·6장
"""


def main():
    # TODO: 구현
    pass


if __name__ == "__main__":
    main()
