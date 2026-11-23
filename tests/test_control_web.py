"""
조종 화면(laptop/control_service/web/) 시험 — 브라우저 없이 볼 수 있는 것만.

- 관제가 화면 파일(/, app.js, style.css)을 주는지
- app.js가 보내는 ui/cmd action과 버튼 조건(allowed) 이름이 인터페이스 정의 6.2와 같은지
- 인터넷 없이 뜨는지 (외부 주소를 불러오지 않음)
화면 모양과 실제 동작은 보연 님이 가짜 작업차·가짜 맵 비전을 띄우고 브라우저로 확인한다 (계획 7-4).
"""

from __future__ import annotations

import pathlib
import re
import urllib.request

from shared import messages as m
from tests.test_control_main import Rig

WEB = pathlib.Path(__file__).resolve().parents[1] / "laptop" / "control_service" / "web"
APP = (WEB / "app.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")


def test_sent_actions_are_in_interface():
    sent = set(re.findall(r'send\("([a-z_]+)"', APP))
    assert sent, "app.js에서 send(...)를 찾지 못함"
    assert sent <= set(m.UI_ACTIONS), sent - set(m.UI_ACTIONS)
    # 화면에서 보낼 수 있어야 하는 명령은 모두 있음 (6.2)
    assert sent == set(m.UI_ACTIONS), set(m.UI_ACTIONS) - sent


def test_set_mode_buttons_match_interface_modes():
    in_html = set(re.findall(r'data-act="set_mode:([a-z]+)"', HTML))
    assert in_html == set(m.UI_SET_MODES)


def test_allowed_keys_used_by_screen_are_real():
    keys = set(re.findall(r'can\("([a-z_:]+)"\)', APP)) | set(re.findall(r'allowed\.includes\("([a-z_:]+)"\)', APP))
    valid = set(m.UI_ACTIONS) - {"set_mode"} | {f"set_mode:{x}" for x in m.UI_SET_MODES}
    assert keys and keys <= valid, keys - valid


def test_works_without_internet():
    for name, text in (("index.html", HTML), ("app.js", APP), ("style.css", CSS)):
        assert not re.search(r'(src|href)=["\']https?://', text), name
        assert "@import" not in text and "fonts.googleapis" not in text, name


def test_space_key_stops_all_before_other_handlers():
    # 캡처 단계(true)에서 받아 입력 칸에 글을 쓰는 중에도 전체 정지 (SR-35, 설계 12장)
    assert re.search(r'addEventListener\("keydown",[\s\S]*?stopAll\(\)[\s\S]*?\}, true\)', APP)
    assert "prompt(" not in APP and "confirm(" not in APP and "alert(" not in APP   # 팝업은 스페이스바를 가로챔


def test_control_serves_screen_files(tmp_path):
    r = Rig(tmp_path)
    try:
        base = f"http://127.0.0.1:{r.net['laptop']['web_port']}"
        for path, must in (("/", "hand-line"), ("/app.js", "ui/cmd"), ("/style.css", "--manual")):
            with urllib.request.urlopen(base + path, timeout=3) as resp:
                assert resp.status == 200
                assert must in resp.read().decode("utf-8")
    finally:
        r.close()


def test_every_hand_ctl_state_has_text():
    # 손 상태 줄 (설계 9.6·9.7, 화면 12장): 관제가 보내는 hand_ctl 값마다 화면 문구가 있어야 함
    block = re.search(r"const HAND_TEXT = \{([\s\S]*?)\n\};", APP).group(1)
    keys = re.findall(r"^\s*([a-z_]+):", block, re.M)
    assert sorted(keys) == sorted(m.HAND_CTL_STATES)


def test_enter_key_allows_release_but_not_in_inputs():
    # Enter = [놓기 허용] (설계 9.7), 이름 입력 칸의 Enter는 이름 저장
    assert re.search(r'e\.key !== "Enter"[\s\S]*?INPUT[\s\S]*?allowRelease\(\)', APP)
