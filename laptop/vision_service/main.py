"""
맵 비전 서비스 (노트북) — 담당: 유정완

하는 일: 맵 위 웹캠에서 ArUco 마커(DICT_4X4_50) 찾기 → 보관함 칸·작업대 마커가 가려졌는지, 콕 LED 켜짐 판단.
        맵 위 영상을 HTTP MJPEG(:8090/video)로 제공.
입력: 맵 위 웹캠, data/calibration/topcam.json, shared/config/map.yaml
출력: vision/state (UDP 127.0.0.1:5101 → 관제 서비스), 맵 위 영상
관련 SR: SR-23, SR-25, SR-28, SR-32
참고: 시스템 설계 5.2, 인터페이스 정의 5.3
"""


def main():
    # TODO: 구현
    pass


if __name__ == "__main__":
    main()
