# SPDX-License-Identifier: GPL-3.0-or-later
"""不接硬件的单元测试：帧编码、回包解析、探测与错误路径。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import driver  # noqa: E402
from driver import SamaS300  # noqa: E402

from lcdmuse.lcd_comm import DeviceOffline, Frame, InvalidArgument  # noqa: E402


def test_build_frame_get_info_bytes():
    # sum(55 AA 07 00 06) = 0x10C
    assert driver.build_frame(driver.CMD_GET_INFO) == bytes.fromhex("55aa0700060c01")


def test_build_frame_screen_off_matches_captured_bytes():
    # 官方软件里抓到的关屏命令
    assert driver.build_frame(driver.CMD_SCREEN_OFF_PARAM, bytes([0x01, 0x0A])) == \
        bytes.fromhex("55aa090014010a 2701".replace(" ", ""))


def test_build_frame_brightness_checksum():
    frame = driver.build_frame(driver.CMD_SET_BRIGHTNESS, bytes([90]))
    assert frame[0:2] == b"\x55\xaa"
    assert frame[2] | (frame[3] << 8) == 8          # len(payload)=1 → 8
    assert frame[4] == driver.CMD_SET_BRIGHTNESS
    assert sum(frame[:-2]) & 0xFFFF == frame[-2] | (frame[-1] << 8)


def test_parse_response_reads_info_json():
    payload = b'{"cmd":"info","data":{"uid":"1","width":960,"height":480,"angle":180}}'
    raw = driver.build_frame(driver.CMD_GET_INFO, b"")[:5] + payload + b"\x00\x00"
    info = driver.parse_response(raw)
    assert info["width"] == 960 and info["angle"] == 180


def test_parse_response_rejects_garbage():
    assert driver.parse_response(b"\x00\x01\x02") is None
    assert driver.parse_response(b"\x55\xaa\x07\x00\x06\x00\x00") is None


def test_probe_returns_none_without_device(monkeypatch):
    monkeypatch.setattr(driver, "find_port", lambda cfg=None: None)
    assert SamaS300.probe({}) is None


def test_probe_reports_expected_geometry(monkeypatch):
    monkeypatch.setattr(driver, "find_port", lambda cfg=None: "COM_TEST")
    info = SamaS300.probe({})
    assert info.width == 960 and info.height == 480
    assert info.native_rotation == 180
    assert info.extra["port"] == "COM_TEST"


def test_push_frame_without_open_raises():
    drv = SamaS300({})
    with pytest.raises(DeviceOffline):
        drv.push_frame(Frame(width=960, height=480, data=b"\x00" * (960 * 480 * 3)))


def test_push_frame_size_mismatch_raises(monkeypatch):
    drv = SamaS300({"auto_rotate": False})
    drv._ser = FakeSerial()          # 不碰真串口

    class _Info:
        width, height = 960, 480
        native_rotation = 180
        capabilities = 0
        extra = {}

    drv._info = _Info()              # type: ignore[assignment]
    with pytest.raises(InvalidArgument):
        drv.push_frame(Frame(width=320, height=240, data=b"\x00" * (320 * 240 * 3)))


class FakeSerial:
    """记录写入的假串口。"""

    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data):
        self.written.append(data)
        return len(data)

    def flush(self):
        pass


def test_push_frame_sends_live_then_jpeg():
    drv = SamaS300({"auto_rotate": False, "jpeg_quality": 70})
    fake = FakeSerial()
    drv._ser = fake

    class _Info:
        width, height = 8, 8
        native_rotation = 0
        capabilities = 0
        extra = {}

    drv._info = _Info()              # type: ignore[assignment]
    drv._size = (8, 8)
    drv._rotation = 0
    drv.push_frame(Frame(width=8, height=8, data=bytes(range(192))))

    assert fake.written[0] == driver.build_frame(driver.CMD_START_LIVE)
    assert fake.written[1][0:2] == b"\xff\xd8"       # JPEG SOI
    assert drv.status()["frames"] == 1
