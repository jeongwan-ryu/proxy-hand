"""shared/config.py 시험: 저장소의 설정 파일, 문서의 예시, 틀린 설정을 막는지."""

import copy
import json
import pathlib
import re

import pytest
import yaml

from shared import config

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "05_인터페이스_정의.md").read_text(encoding="utf-8")


def doc_block(heading: str, lang: str, nth: int = 0) -> str:
    """docs/05의 제목 아래 nth번째 코드 블록."""
    start = DOC.index(heading)
    end = DOC.find("\n### ", start + 1)
    part = DOC[start:end if end != -1 else None]
    return re.findall(rf"```{lang}\n(.*?)```", part, re.S)[nth]


def key_tree(v):
    """값은 빼고 키 구조만 (정거장 목록은 id 기준)."""
    if isinstance(v, dict):
        return {str(k): key_tree(x) for k, x in v.items()}
    if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
        return {x["id"]: key_tree(x) for x in v}
    return None


def write_yaml(tmp_path, data, name="x.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# --- 저장소에 있는 설정 파일 ------------------------------------------------------

def test_repo_files_load():
    config.load_network()
    config.load_map()
    config.load_laptop()
    config.load_car(require_tuned=False)


def test_car_untuned_is_refused_for_service_start():
    with pytest.raises(config.ConfigError, match="speed_normal"):
        config.load_car()


@pytest.mark.parametrize("heading, path", [
    ("### 9.1", config.NETWORK_YAML),
    ("### 9.2", config.MAP_YAML),
    ("### 9.3", config.CAR_YAML),
    ("### 9.4", config.LAPTOP_YAML),
])
def test_repo_files_match_doc_keys(heading, path):
    doc = yaml.safe_load(doc_block(heading, "yaml"))
    repo = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert key_tree(repo) == key_tree(doc), f"{path.name}와 docs/05 {heading} 예시의 필드가 다름"


# --- 문서의 보정 파일 예시 ----------------------------------------------------------

def test_doc_calibration_examples_are_valid(tmp_path):
    (tmp_path / "aihand_A.json").write_text(doc_block("### 9.5", "json"), encoding="utf-8")
    (tmp_path / "topcam.json").write_text(doc_block("### 9.6", "json", 0), encoding="utf-8")
    (tmp_path / "operator.json").write_text(doc_block("### 9.6", "json", 1), encoding="utf-8")
    assert config.load_aihand("aihand_A", tmp_path)["drive_wrist"] == 0
    assert config.load_topcam(tmp_path)["cock_led"]["on_threshold"] == 143.5
    assert len(config.load_operator(tmp_path)["open"]) == 5


def test_missing_calibration_is_none(tmp_path):
    assert config.load_aihand("aihand_B", tmp_path) is None
    assert config.load_topcam(tmp_path) is None
    assert config.load_operator(tmp_path) is None


def test_network_calibration_presence(tmp_path):
    (tmp_path / "aihand_A.json").write_text(doc_block("### 9.5", "json"), encoding="utf-8")
    net = config.load_network()
    assert config.check_network_against_calibration(net, tmp_path) == {"1": True, "2": False}


def test_bad_aihand_is_refused(tmp_path):
    data = json.loads(doc_block("### 9.5", "json"))
    data["servos"][5]["center"] = 200
    (tmp_path / "aihand_A.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="servos"):
        config.load_aihand("aihand_A", tmp_path)


def test_led_rect_outside_image_is_refused(tmp_path):
    data = json.loads(doc_block("### 9.6", "json", 0))
    data["cock_led"]["rect"] = [1910, 405, 24, 24]
    (tmp_path / "topcam.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="rect"):
        config.load_topcam(tmp_path)


# --- 맵 도우미 ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def mp():
    return config.load_map()


@pytest.mark.parametrize("a, b, n", [
    ("wait_1", "storage_2", 2),
    ("cock", "wait_1", 1),      # 고리를 한 바퀴 돌아 대기로
    ("bench_1", "storage_1", 3),
    ("wait_2", "meter", 2),
    ("meter", "wait_2", 1),
    ("cock", "cock", 0),
])
def test_lines_to(mp, a, b, n):
    assert mp.lines_to(a, b) == n


def test_lines_to_other_loop(mp):
    with pytest.raises(ValueError):
        mp.lines_to("wait_1", "meter")


def test_map_helpers(mp):
    assert mp.wait_station(2) == "wait_2"
    assert set(mp.stations_of_car(2)) == {"wait_2", "bench_2", "meter"}
    assert mp.is_station("cock") and not mp.is_station("storage_*")
    assert mp.line_check_enabled() is False      # segment_s가 아직 0


# --- 틀린 맵을 막는지 ----------------------------------------------------------------

@pytest.fixture
def raw_map():
    return yaml.safe_load(config.MAP_YAML.read_text(encoding="utf-8"))


def broken(raw, fn):
    d = copy.deepcopy(raw)
    fn(d)
    return d


@pytest.mark.parametrize("why, change", [
    ("없는 대기 정거장", lambda d: d["cars"]["1"].update(wait_station="wait_9")),
    ("다른 고리의 대기 정거장", lambda d: d["cars"]["1"].update(wait_station="wait_2")),
    ("출발 조건 오타", lambda d: d["cars"]["2"].update(start_when="bench_full")),
    ("두 작업차가 같은 고리", lambda d: d["cars"]["2"].update(loop="dispense", wait_station="wait_1")),
    ("정거장 id 겹침", lambda d: d["stations"][1].update(id="wait_1")),
    ("정지선 번호 범위 밖", lambda d: d["stations"][5].update(line=6)),
    ("정지선 번호 겹침", lambda d: d["stations"][2].update(line=1)),
    ("정지선 수와 정거장 수가 다름", lambda d: d["loops"].update(dispense=7)),
    ("마커 ID 겹침", lambda d: d["markers"].update(bench_id=1)),
    ("필드 빠짐", lambda d: d["vision"].pop("led_on_s")),
    ("타입 틀림", lambda d: d["drive_pose"].update(enabled="yes")),
    ("작업차 키가 숫자", lambda d: d.update(cars={1: d["cars"]["1"], 2: d["cars"]["2"]})),
])
def test_bad_map_is_refused(tmp_path, raw_map, why, change):
    path = write_yaml(tmp_path, broken(raw_map, change))
    with pytest.raises(config.ConfigError):
        config.load_map(path)


def test_error_message_names_file_and_field(tmp_path, raw_map):
    path = write_yaml(tmp_path, broken(raw_map, lambda d: d["auto"].pop("goto_timeout_s")), "map.yaml")
    with pytest.raises(config.ConfigError) as e:
        config.load_map(path)
    assert "map.yaml" in str(e.value) and "auto.goto_timeout_s" in str(e.value)


def test_bad_network_is_refused(tmp_path):
    raw = yaml.safe_load(config.NETWORK_YAML.read_text(encoding="utf-8"))
    raw["timing"]["heartbeat_s"] = 2.0
    with pytest.raises(config.ConfigError, match="heartbeat_s"):
        config.load_network(write_yaml(tmp_path, raw))


def test_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="파일이 없음"):
        config.load_laptop(tmp_path / "nope.yaml")


def test_or_exit_stops_service(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        config.or_exit(config.load_laptop, tmp_path / "nope.yaml")
    assert e.value.code == 1
    assert "[설정 오류]" in capsys.readouterr().err
