"""
약속 시험: docs/05_인터페이스_정의.md 와 shared/messages.py 가 같은지 확인한다.

문서를 고쳤는데 코드를 안 고쳤거나, 코드를 고쳤는데 문서를 안 고쳤으면 여기서 실패한다.
실패 메시지에 어느 쪽에만 있는 이름인지 나온다.
"""

import json
import pathlib
import re

import pytest

from shared import messages as m

DOC = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "05_인터페이스_정의.md").read_text(encoding="utf-8")


def section(heading: str) -> str:
    """'### 5.1' 처럼 시작하는 제목부터 같은 높이 이상의 다음 제목 전까지."""
    level = heading.split(" ")[0]
    lines = DOC.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    out = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            this_level = line.split(" ")[0]
            if len(this_level) <= len(level):
                break
        out.append(line)
    return "\n".join(out)


def cells(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def table_rows(text: str, header: bool = False):
    """마크다운 표의 본문 줄들을 칸 목록으로. header=True면 제목 줄만."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        is_header = i + 1 < len(lines) and lines[i + 1].startswith("| ---")
        if is_header == header:
            yield cells(line)


def first_code(cell: str) -> str:
    found = re.match(r"`([^`]+)`", cell)
    return found.group(1) if found else ""


def field_table(heading: str) -> set:
    """'| 필드 | 타입 | 뜻 |' 표에서 필드 이름들."""
    return {first_code(r[0]) for r in table_rows(section(heading)) if first_code(r[0])}


def diff_msg(doc: set, code: set) -> str:
    return f"문서에만: {sorted(doc - code)}, 코드에만: {sorted(code - doc)}"


# --- 이름 목록 ---------------------------------------------------------------

def test_car_topics_match_doc():
    doc = set(re.findall(r"`car\{n\}/(\w+)`", section("### 4.1") + section("### 4.2")))
    code = set(m.CAR_MESSAGES)
    assert doc == code, diff_msg(doc, code)


def test_error_codes_match_doc():
    rows = [r for r in table_rows(section("## 8.")) if first_code(r[0])]
    doc_all = {first_code(r[0]) for r in rows}
    doc_pi = {first_code(r[0]) for r in rows if "작업차 서비스" in r[1]}
    assert doc_all == set(m.ALL_CODES), diff_msg(doc_all, set(m.ALL_CODES))
    assert doc_pi == set(m.PI_ERROR_CODES), diff_msg(doc_pi, set(m.PI_ERROR_CODES))


def test_ui_actions_match_doc():
    doc = {first_code(r[0]) for r in table_rows(section("### 6.2")) if first_code(r[0])}
    assert doc == set(m.UI_ACTIONS), diff_msg(doc, set(m.UI_ACTIONS))


def test_ui_action_fields_match_doc():
    for row in table_rows(section("### 6.2")):
        action = first_code(row[0])
        if not action:
            continue
        # "`car`, `task`: 작업 `id`, `repeat`: 불" → car, task, repeat (설명 속 `id`는 빼기)
        doc_fields = set(re.findall(r"(?:^|, )`(\w+)`", row[1]))
        code_fields = set(m.UI_ACTIONS[action])
        assert doc_fields == code_fields, f"{action}: " + diff_msg(doc_fields, code_fields)


# --- 필드 목록 ---------------------------------------------------------------

def test_car_message_fields_match_doc():
    for row in table_rows(section("### 4.1") + "\n" + section("### 4.2")):
        found = re.match(r"`car\{n\}/(\w+)`", row[0])
        if not found:
            continue
        name = found.group(1)
        field_cell = row[3]
        doc_fields = set(re.findall(r"`(\w+)`:", field_cell))
        required, optional = m.CAR_FIELDS[name]
        code_fields = set(required) | set(optional)
        assert doc_fields == code_fields, f"car{{n}}/{name}: " + diff_msg(doc_fields, code_fields)


@pytest.mark.parametrize("heading, topic", [
    ("### 5.1", m.HAND_POSE),
    ("### 5.2", m.HAND_CALIB),
    ("### 5.3", m.VISION_STATE),
])
def test_internal_fields_match_doc(heading, topic):
    doc = field_table(heading)
    code = set(m.FIXED_FIELDS[topic][0])
    assert doc == code, f"{topic}: " + diff_msg(doc, code)


def test_ui_state_fields_match_doc():
    text = section("### 6.1")
    top, cars, alert = text.split("`cars` 안의 값")[0], *text.split("`cars` 안의 값")[1].split("**`ui/alert`**")
    cars, hand_ctl = cars.split("`hand_ctl` 값")
    top_doc = {first_code(r[0]) for r in table_rows(top) if first_code(r[0])}
    cars_doc = {first_code(r[0]) for r in table_rows(cars) if first_code(r[0])}
    hand_ctl_doc = [first_code(r[0]) for r in table_rows(hand_ctl) if first_code(r[0])]
    assert hand_ctl_doc == list(m.HAND_CTL_STATES), f"hand_ctl 값 (순서까지): 문서 {hand_ctl_doc}, 코드 {list(m.HAND_CTL_STATES)}"
    alert_doc = {first_code(r[0]) for r in table_rows(alert) if first_code(r[0])}
    assert top_doc == set(m.FIXED_FIELDS[m.UI_STATE][0]), diff_msg(top_doc, set(m.FIXED_FIELDS[m.UI_STATE][0]))
    assert cars_doc == set(m.CAR_STATE_FIELDS), diff_msg(cars_doc, set(m.CAR_STATE_FIELDS))
    assert alert_doc == set(m.FIXED_FIELDS[m.UI_ALERT][0]), diff_msg(alert_doc, set(m.FIXED_FIELDS[m.UI_ALERT][0]))


# --- 파이가 상태별로 받는 명령 (4.4) ----------------------------------------------

def test_pi_command_policy_matches_doc():
    text = section("### 4.4").split("**상태별로 받는 명령**")[1]
    symbol = {"O": m.ACCEPT, "X": m.REJECT_CMD, "—": m.DROP}
    rows = [r for r in table_rows(text) if first_code(r[0])]
    header = [first_code(c) for c in next(table_rows(text, header=True))[1:]]
    assert tuple(header) == m.PI_STATES
    doc = {first_code(r[0]): dict(zip(header, (symbol[c] for c in r[1:]))) for r in rows}
    assert doc == m.PI_CMD_POLICY


# --- 문서의 예시 메시지가 검사를 통과하는지 -------------------------------------------

def doc_examples():
    for block in re.findall(r"```json\n(.*?)```", DOC, re.S):
        for line in block.strip().split("\n"):
            if line.startswith('{"topic"'):
                yield line


@pytest.mark.parametrize("line", list(doc_examples()))
def test_doc_examples_are_valid(line):
    msg = m.decode(line)
    assert msg == json.loads(line)


def test_doc_has_examples():
    assert len(list(doc_examples())) >= 8
