"""shared/mjpeg.py 시험: 브라우저처럼 접속해 마지막 사진을 받는지."""

import socket

from shared.mjpeg import MjpegServer


def _get(port, path):
    s = socket.create_connection(("127.0.0.1", port), timeout=2)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    return s


def test_stream_sends_latest_frame():
    server = MjpegServer(0, host="127.0.0.1")
    server.start()
    try:
        server.publish(b"old")
        server.publish(b"\xff\xd8jpeg\xff\xd9")
        s = _get(server.port, "/video")
        data = b""
        while b"\xff\xd9" not in data:
            data += s.recv(4096)
        s.close()
        assert b"multipart/x-mixed-replace; boundary=frame" in data
        assert b"Content-Length: 8\r\n\r\n\xff\xd8jpeg\xff\xd9" in data
        assert b"old" not in data
    finally:
        server.close()


def test_other_path_is_404():
    server = MjpegServer(0, host="127.0.0.1")
    server.start()
    try:
        s = _get(server.port, "/")
        assert s.recv(100).startswith(b"HTTP/1.0 404")
        s.close()
    finally:
        server.close()
