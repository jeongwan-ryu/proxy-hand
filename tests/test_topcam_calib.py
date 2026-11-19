"""맵 위 웹캠 보정 도구 시험: 밝기 재기, 저장한 파일을 config.load_topcam이 받는지 (웹캠 없이)."""

import numpy as np
import pytest

from laptop.tools import topcam_calib as tc
from shared import config


def test_mean_brightness_of_rect_only():
    img = np.zeros((1080, 1920, 3), np.uint8)
    img[100:110, 200:210] = 255
    assert tc.mean_brightness(img, [200, 100, 10, 10]) == pytest.approx(255, abs=1)
    assert tc.mean_brightness(img, [0, 0, 10, 10]) == 0


def test_threshold_is_middle_and_file_loads(tmp_path):
    data = tc.make_topcam(1, 1920, 1080, [1210, 405, 24, 24], on_level=220.0, off_level=67.0)
    assert data["cock_led"]["on_threshold"] == 143.5
    tc.save_topcam(data, tmp_path)
    loaded = config.load_topcam(tmp_path)
    assert loaded["camera_index"] == 1 and loaded["cock_led"]["rect"] == [1210, 405, 24, 24]


def test_too_small_gap_is_refused():
    with pytest.raises(ValueError, match="차이"):
        tc.make_topcam(1, 1920, 1080, [0, 0, 10, 10], on_level=90.0, off_level=80.0)


def test_rect_outside_picture_is_not_saved(tmp_path):
    data = tc.make_topcam(1, 1920, 1080, [1915, 0, 24, 24], on_level=200.0, off_level=50.0)
    with pytest.raises(config.ConfigError):
        tc.save_topcam(data, tmp_path)
    assert not (tmp_path / "topcam.json").exists()
