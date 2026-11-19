"""
손 인식 서비스 (노트북) — 담당: 이보연

하는 일: 내장 웹캠 사진 → MediaPipe 손 관절 점 21개 → 손가락 굽힘 정도 r(0~1), 손목 회전 각도.
입력: 내장 웹캠, hand/calib (UDP 5102), laptop/config/laptop.yaml, data/calibration/operator.json
출력: hand/pose (UDP 127.0.0.1:5100 → 관제 서비스),
      손 영상 (HTTP MJPEG :8091/video → 브라우저. 거울 사진 + 관절 점, 320×240, 1초 10장)
관련 SR: SR-01, SR-09, SR-32
참고: 시스템 설계 9.2·12장, 인터페이스 정의 2장·5.1·5.2
"""


def main():
    # TODO: 구현
    pass


if __name__ == "__main__":
    main()
