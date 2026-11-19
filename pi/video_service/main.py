"""
영상 서비스 (각 파이) — 담당: 유정완

하는 일: Raspbot 카메라 영상을 HTTP MJPEG(:8080/video)로 제공.
관련 SR: SR-32
참고: 인터페이스 정의 2장·9.3 (car.yaml의 video)

실행 (저장소 맨 위에서):  python -m pi.video_service.main
작업차 서비스와 따로 돈다. 카메라가 빠져도 1초마다 다시 열고, 그동안 보는 화면은 마지막 사진에 멈춰 있음.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import cv2

from shared import config
from shared.mjpeg import MjpegServer

JPEG_QUALITY = 70
REOPEN_S = 1.0


def open_camera(video: Dict[str, Any]) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(video["device"])
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, video["width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, video["height"])
    cap.set(cv2.CAP_PROP_FPS, video["fps"])
    return cap


def run(video: Dict[str, Any], server: MjpegServer, cap_factory=open_camera,
        should_stop=lambda: False, clock=time.monotonic, sleep=time.sleep) -> None:
    """카메라 → JPEG → server. fps보다 빨리 보내지 않음."""
    period = 1.0 / video["fps"]
    cap: Optional[cv2.VideoCapture] = cap_factory(video)
    next_frame = clock()
    while not should_stop():
        ok, frame = cap.read() if cap is not None else (False, None)
        if not ok:
            print(f"[카메라 오류] /dev/video{video['device']}에서 사진이 안 옴. {REOPEN_S}초 뒤 다시 엶")
            if cap is not None:
                cap.release()
            sleep(REOPEN_S)
            cap = cap_factory(video)
            continue
        now = clock()
        if now < next_frame:
            continue                      # 카메라가 설정보다 빨리 줄 때 버림
        next_frame = now + period
        if frame.shape[1] != video["width"]:   # 카메라가 크기 설정을 무시했을 때
            frame = cv2.resize(frame, (video["width"], video["height"]))
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if ok:
            server.publish(jpg.tobytes())


def main() -> None:
    car = config.or_exit(config.load_car, require_tuned=False)   # 영상은 속도 값이 필요 없음
    video = car["video"]
    net = config.or_exit(config.load_network)
    if net["cars"][str(car["car_id"])]["video_port"] != video["port"]:
        print(f"[설정 확인] car.yaml video.port({video['port']})와 network.yaml cars.{car['car_id']}.video_port"
              f"({net['cars'][str(car['car_id'])]['video_port']})가 다름. 조종 화면은 network.yaml 값으로 접속함")
    server = MjpegServer(video["port"])
    server.start()
    print(f"영상 서비스: 작업차 {car['car_id']}, /dev/video{video['device']} "
          f"{video['width']}×{video['height']} {video['fps']}fps → http://<파이 IP>:{video['port']}/video")
    run(video, server)


if __name__ == "__main__":
    main()
