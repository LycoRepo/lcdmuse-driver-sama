# SPDX-License-Identifier: GPL-3.0-or-later
"""先马 S300 水冷冷头屏驱动（6 英寸 IPS，960×480，CDC 串口）。

协议是实测出来的，不是猜的：

    帧 = 55 AA | len_lo len_hi (= len(payload) + 7) | cmd | payload | checksum(sum & 0xFFFF, LE)

    cmd 6     取设备信息，回一段 JSON（含真实分辨率、型号、angle、brightness、version）
    cmd 17    进实时模式，同时当保活用
    cmd 3     设亮度
    cmd 33    关屏
    cmd 0x14  关屏（带参），实测字节 55 AA 09 00 14 01 0A 27 01
    推图      先发 FF D9 FF D9 停画面 → cmd 6 → cmd 17 → 直接灌 JPEG 原始字节（无帧封装）

设备的 960×480 面板是倒装的（`angle: 180`），所以驱动负责把逻辑方向的帧转 180° 再编码；
关掉 `auto_rotate` 就按原样发。
"""
from __future__ import annotations

import io
import json
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from lcdmuse.lcd_comm import (Capability, DeviceBusy, DeviceInfo, DeviceOffline, Frame, InvalidArgument,
                              LcdComm, Unsupported)

VID = 0x33C3
PID = 0x7803
BAUDRATE = 921600
NATIVE_ROTATION = 180          # 面板倒装
DEFAULT_SIZE = (960, 480)

CMD_GET_INFO = 6
CMD_SET_BRIGHTNESS = 3
CMD_SCREEN_OFF = 33
CMD_SCREEN_OFF_PARAM = 0x14
CMD_START_LIVE = 17

STOP_MARKER = b"\xff\xd9\xff\xd9"


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """组一帧：55 AA + len + cmd + payload + checksum(LE)。"""
    body = bytes([0x55, 0xAA]) + (len(payload) + 7).to_bytes(2, "little") + bytes([cmd]) + payload
    return body + (sum(body) & 0xFFFF).to_bytes(2, "little")


def parse_response(raw: bytes) -> dict[str, Any] | None:
    """从设备回来的帧里取出 JSON；不是 JSON 就返回 None。"""
    if len(raw) < 7 or raw[0:2] != b"\x55\xaa":
        return None
    body = raw[5:-2]
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    node = data.get("data") if isinstance(data, dict) else None
    return node if isinstance(node, dict) else (data if isinstance(data, dict) else None)


def find_port(config: Mapping[str, Any] | None = None) -> str | None:
    """找屏：配置里指定了就用它，否则按 VID/PID 扫串口。"""
    cfg = config or {}
    wanted = str(cfg.get("port") or "").strip()
    if wanted:
        return wanted
    try:
        from serial.tools import list_ports
    except Exception:
        return None
    for port in list_ports.comports():
        if port.vid == VID and port.pid == PID:
            return port.device
    return None


class SamaS300(LcdComm):
    driver_id: ClassVar[str] = "sama_s300"
    display_name: ClassVar[str] = "先马 S300"

    def __init__(self, config: Mapping[str, Any] | None = None):
        super().__init__(config)
        self._ser = None
        self._size = DEFAULT_SIZE
        self._rotation = NATIVE_ROTATION
        self._last_keepalive = 0.0
        self._frames = 0

    # ── 探测 ────────────────────────────────────────────────────────────
    @classmethod
    def probe(cls, config: Mapping[str, Any] | None = None) -> DeviceInfo | None:
        port = find_port(config)
        if not port:
            return None
        return DeviceInfo(
            id=port,
            name="先马 S300（水冷头屏）",
            width=DEFAULT_SIZE[0],
            height=DEFAULT_SIZE[1],
            shape="rect",
            native_rotation=NATIVE_ROTATION,
            capabilities=(Capability.PUSH_FULL | Capability.BRIGHTNESS | Capability.POWER
                          | Capability.KEEPALIVE),
            firmware="",
            extra={"port": port, "baud": BAUDRATE, "note": "分辨率以 cmd 6 实回为准"},
        )

    # ── 打开 ────────────────────────────────────────────────────────────
    def open(self) -> DeviceInfo:
        import serial

        port = find_port(self.config)
        if not port:
            raise DeviceOffline("没找到先马 S300（USB 33C3:7803）")

        baud = int(self.config.get("baud") or BAUDRATE)
        try:
            self._ser = serial.Serial(port, baud, timeout=float(self.config.get("timeout") or 1.5),
                                      write_timeout=float(self.config.get("write_timeout") or 5.0))
        except Exception as exc:
            raise DeviceBusy(f"{port} 打不开（多半被 SAMA.exe 占着）: {exc}") from exc

        try:
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
            self._ser.write(STOP_MARKER)
            self._ser.flush()
            time.sleep(0.25)

            info = self._query_info()
            if info:
                self._size = (int(info.get("width") or DEFAULT_SIZE[0]),
                              int(info.get("height") or DEFAULT_SIZE[1]))
                self._rotation = int(info.get("angle") if info.get("angle") is not None else NATIVE_ROTATION)

            self._start_live()
            return self.bind(DeviceInfo(
                id=str((info or {}).get("uid") or port),
                name=str((info or {}).get("model") or "先马 S300（水冷头屏）"),
                width=self._size[0],
                height=self._size[1],
                shape="rect",
                native_rotation=self._rotation,
                capabilities=(Capability.PUSH_FULL | Capability.BRIGHTNESS | Capability.POWER
                              | Capability.KEEPALIVE),
                firmware=str((info or {}).get("version") or ""),
                extra={"port": port, "baud": baud, "region": (info or {}).get("region"),
                       "brightness": (info or {}).get("brightness")},
            ))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    # ── 送帧 ────────────────────────────────────────────────────────────
    def push_frame(self, frame: Frame) -> None:
        ser = self._require_serial()
        img = frame.to_image()
        if self.config.get("auto_rotate", True) and self._rotation % 360:
            img = img.rotate(-self._rotation % 360, expand=False)
        if img.size != self._size:
            raise InvalidArgument(f"帧尺寸 {img.size} 与设备 {self._size} 不一致")

        quality = int(self.config.get("jpeg_quality") or 88)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=False)
        jpeg = buf.getvalue()

        try:
            ser.write(build_frame(CMD_START_LIVE))
            ser.write(jpeg)
            ser.flush()
        except Exception as exc:
            raise DeviceOffline(f"推帧失败: {exc}") from exc
        self._frames += 1
        self._last_keepalive = time.time()

    # ── 可选能力 ────────────────────────────────────────────────────────
    def set_brightness(self, percent: int) -> None:
        value = max(0, min(100, int(percent)))
        self._send(build_frame(CMD_SET_BRIGHTNESS, bytes([value])))

    def power(self, on: bool) -> None:
        if on:
            # 唤醒没有公开命令；用「停画面 + 实时模式」把它重新点亮
            self._send(STOP_MARKER)
            time.sleep(0.15)
            self._send(build_frame(CMD_GET_INFO))
            self._start_live()
            return
        # 实测字节：55 AA 09 00 14 01 0A 27 01
        self._send(build_frame(CMD_SCREEN_OFF_PARAM, bytes([0x01, 0x0A])))

    def push_region(self, frame: Frame, x: int, y: int) -> None:
        raise Unsupported("先马 S300 只能整帧推 JPEG，没有区域绘制")

    def tick(self, now: float) -> None:
        """定时重发 cmd 17，别让屏自己睡过去。"""
        interval = float(self.config.get("keepalive_seconds") or 2.0)
        if self._ser is not None and now - self._last_keepalive >= interval:
            self._start_live()

    def status(self) -> dict[str, Any]:
        return {"driver": self.driver_id, "port": self.config.get("port"),
                "size": list(self._size), "rotation": self._rotation,
                "frames": self._frames, "open": self._ser is not None}

    # ── 内部 ────────────────────────────────────────────────────────────
    def _require_serial(self):
        if self._ser is None:
            raise DeviceOffline("设备还没 open")
        return self._ser

    def _send(self, data: bytes) -> None:
        ser = self._require_serial()
        try:
            ser.write(data)
            ser.flush()
        except Exception as exc:
            raise DeviceOffline(f"写串口失败: {exc}") from exc

    def _start_live(self) -> None:
        self._send(build_frame(CMD_START_LIVE))
        self._last_keepalive = time.time()

    def _query_info(self) -> dict[str, Any] | None:
        ser = self._require_serial()
        ser.write(build_frame(CMD_GET_INFO))
        ser.flush()
        head = ser.read(5)
        if len(head) < 5 or head[0:2] != b"\x55\xaa":
            return None
        total = head[2] | (head[3] << 8)
        rest = max(0, total - 5)
        return parse_response(head + (ser.read(rest) if rest else b""))
