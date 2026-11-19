"""
HTTP MJPEG 영상 서버 (노트북·파이 공통). 브라우저가 <img src="http://.../video">로 바로 봄.

맵 비전 서비스(:8090)와 영상 서비스(:8080)가 같이 쓴다 (인터페이스 정의 2장).
JPEG로 바꾸는 일은 부르는 쪽이 한다 (cv2.imencode). 여기는 표준 라이브러리만 쓴다.

    server = MjpegServer(8090)
    server.start()
    server.publish(jpeg_bytes)    # 새 사진이 생길 때마다. 보는 사람에게는 가장 최근 것만 감

파이의 Python 버전에 맞추기 위해 3.9 문법까지만 쓴다.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

PATH = "/video"
BOUNDARY = b"frame"


class MjpegServer:
    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self._frame: Optional[bytes] = None
        self._count = 0
        self._cond = threading.Condition()
        self._closed = False
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server 이름 규칙)
                if self.path.split("?")[0] != PATH:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY.decode())
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                seen = -1
                try:
                    while True:
                        frame, seen = server._wait_frame(seen)
                        if frame is None:
                            return
                        self.wfile.write(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    return   # 브라우저가 닫음

            def log_message(self, *args: object) -> None:
                pass         # 요청마다 찍지 않음

        self._http = ThreadingHTTPServer((host, port), Handler)
        self._http.daemon_threads = True

    @property
    def port(self) -> int:
        return self._http.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self._http.serve_forever, name="mjpeg", daemon=True).start()

    def publish(self, jpeg: bytes) -> None:
        with self._cond:
            self._frame = jpeg
            self._count += 1
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._http.shutdown()
        self._http.server_close()

    def _wait_frame(self, seen: int):
        """seen 다음 사진이 생길 때까지 기다림. 닫히면 (None, seen)."""
        with self._cond:
            while self._count == seen and not self._closed:
                self._cond.wait(1.0)
            if self._closed:
                return None, seen
            return self._frame, self._count
