# lcdmuse-driver-sama — 先马 S300 水冷头屏驱动

给 [lcdmuse](https://github.com/LycoRepo/lcdmuse) 用的驱动插件：驱动**先马 S300** 一体式水冷冷头上的 6 英寸 IPS 屏（960×480，USB CDC 串口）。

```
pip install pyserial
lcdmuse plugin add https://github.com/LycoRepo/lcdmuse-driver-sama   # 或下载后 plugin add <目录>
lcdmuse drivers list      # 应看到 sama_s300，available: true
lcdmuse probe             # 接上屏后能报出 COM 口
lcdmuse render --plugin sama_s300 --out out/screen.png
```

## 协议（实测，不是猜的）

```
帧 = 55 AA | len_lo len_hi (= len(payload) + 7) | cmd | payload | checksum(sum & 0xFFFF, LE)
```

| cmd | 作用 | 备注 |
|---|---|---|
| `6` | 取设备信息 | 返回 JSON：`width/height/model/angle/brightness/version/uid` |
| `17` | 进实时模式 | 同时当保活用，驱动每 2 秒重发一次 |
| `3` | 设亮度 | payload 一个字节 0–100 |
| `33` | 关屏 | |
| `0x14` | 关屏（带参） | 实测字节 `55 AA 09 00 14 01 0A 27 01` |
| — | 推图 | 先发 `FF D9 FF D9` 停画面 → `cmd 6` → `cmd 17` → 直接灌 JPEG 字节（无帧封装） |

串口 921600 8N1。面板是**倒装的**（设备自报 `angle: 180`），驱动会把帧转 180° 再编码；
不想要就设 `auto_rotate: false`。

## 带宽与帧率

921600 8N1 = 92,160 B/s。960×480 JPEG（质量 88）约 30 KB/帧 → 约 **3 fps**。
降低 `jpeg_quality` 能到 5–6 fps，画质换来的。这块屏没有区域刷新的命令，只能整帧。

## 注意

- **同一时间只能有一个程序占串口**。先马官方软件（`SAMA.exe`）以管理员权限运行并独占串口，
  跑本驱动前要先把它关掉（需要管理员权限），否则驱动会报 `device_busy`。
- 关掉官方软件后，冷头的泵和风扇按固件默认策略运行；如果你依赖官方软件的曲线控制，
  验证完记得把它开回来。
- 驱动不会改官方软件的任何文件。

## 配置

`driver.yaml` 里 `settings_schema` 可配：`port`（留空自动按 USB `33C3:7803` 找）、`baud`、
`jpeg_quality`、`auto_rotate`、`keepalive_seconds`。

## 测试

```bash
pip install -r requirements-dev.txt   # pytest + lcdmuse
PYTHONPATH=/path/to/lcdmuse pytest tests -q
```

10 个用例覆盖帧编码（含从官方软件里抓到的关屏命令）、回包解析、探测、错误路径。

## 许可

GPL-3.0-or-later。协议描述来自对设备的实际抓包与探测，属互操作性说明。
