#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步采集器：一边录系统音频，一边收 GSI，两者共享同一个时钟做到样本级对齐。

为什么需要它
------------
现有的 35 个录音素材无法用来标定阈值：它们是 2~5 秒的窗口，每个窗口里同时有多个
声音；"拆除成功声"的窗口里本来就包含"拆弹开始"的声音，所以高分可能是真阳性。
唯一能给出可靠标注的办法是：**用 GSI 的真实事件时间去标注真实对局的音频**。
观战时 GSI 会给出 `bomb.state=defusing` 的精确时刻（实测 0.25s 一个包），
死一次就能拿到一次"拆弹开始"的标注。

对齐原理
--------
音频线程累加已写入的帧数 `total_frames`；GSI 回调在收到包的那一刻读取它，
并把 `(recv_mono, total_frames)` 一起写盘。于是任何一个 GSI 事件都能换算成
WAV 里的精确样本位置：`sample = total_frames`（误差 <= 一个音频块，约 10ms）。

产出
----
    sessions/session_YYYYmmdd_HHMMSS/
        audio.wav        单声道 16bit 48kHz
        gsi.jsonl        每包一行，含 total_frames / mono / 解析摘要 / 原始 body
        meta.json        环境信息（设备、采样率、时长、包数）
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                                # 为了 import c4timer.gsi（产品代码）

from c4timer.audio.capture import LoopbackCapture  # noqa: E402
from c4timer.audio.detect import ANALYSIS_RATE  # noqa: E402
from c4timer.audio.loopback import list_render_devices  # noqa: E402
from c4timer.config import AudioConfig  # noqa: E402
from c4timer.gsi import GsiReceiver, parse_payload  # noqa: E402

SESSIONS = ROOT / "sessions"


class StreamingWav:
    """边采边写，不在内存里攒整段。收尾时回填 RIFF/data 长度。"""

    def __init__(self, path: Path, rate: int, channels: int = 1, sampwidth: int = 2):
        self.path = path
        self.rate = rate
        self.channels = channels
        self.sampwidth = sampwidth
        self.fh = open(path, "wb")
        self.fh.write(b"RIFF" + b"\0\0\0\0" + b"WAVE")
        self.fh.write(b"fmt " + struct.pack(
            "<IHHIIHH", 16, 1, channels, rate,
            rate * channels * sampwidth, channels * sampwidth, sampwidth * 8))
        self.fh.write(b"data" + b"\0\0\0\0")
        self.data_bytes = 0
        self.frames = 0
        self._lock = threading.Lock()

    def write(self, mono: np.ndarray) -> int:
        pcm = np.clip(mono, -1.0, 1.0)
        data = (pcm * 32767.0).astype("<i2").tobytes()
        with self._lock:
            self.fh.write(data)
            self.data_bytes += len(data)
            self.frames += int(mono.size)
            return self.frames

    def position(self) -> int:
        with self._lock:
            return self.frames

    def patch_header(self) -> None:
        """把当前长度写回 RIFF/data 头。

        为什么需要：用户很可能直接关窗口而不是按 Ctrl+C。不做周期性回填的话，
        那种情况下 WAV 头里的长度还是 0，整个录音作废。每几秒回填一次，
        即使被强杀，文件也至少是到上一次回填为止的合法 WAV。
        """
        with self._lock:
            end = self.fh.tell()
            self.fh.seek(4)
            self.fh.write(struct.pack("<I", 36 + self.data_bytes))
            self.fh.seek(40)
            self.fh.write(struct.pack("<I", self.data_bytes))
            self.fh.seek(end)
            self.fh.flush()

    def close(self) -> None:
        with self._lock:
            self.fh.seek(4)
            self.fh.write(struct.pack("<I", 36 + self.data_bytes))
            self.fh.seek(40)
            self.fh.write(struct.pack("<I", self.data_bytes))
            self.fh.flush()
            self.fh.close()


class Session:
    def __init__(self, outdir: Path, rate: int) -> None:
        self.outdir = outdir
        self.rate = rate
        self.wav = StreamingWav(outdir / "audio.wav", rate)
        self.gsi_fh = open(outdir / "gsi.jsonl", "a", encoding="utf-8")
        self._lock = threading.Lock()
        self.gsi_count = 0
        self.observer_count = 0
        self.t0_mono = time.monotonic()
        self.labels: list[dict] = []

    def on_pcm(self, mono: np.ndarray) -> None:
        self.wav.write(mono)

    def on_sample(self, sample) -> None:
        frames = self.wav.position()
        with self._lock:
            self.gsi_count += 1
            if sample.observer:
                self.observer_count += 1
            rec = {
                "n": self.gsi_count,
                "mono": round(sample.recv_mono - self.t0_mono, 4),
                "frames": frames,
                "t_audio": round(frames / self.rate, 4),
                "observer": sample.observer,
                "round_phase": sample.round_phase,
                "map_round": sample.map_round,
                "bomb_state": sample.bomb_state,
                "bomb_state_raw": sample.bomb_state_raw,
                "round_bomb": sample.round_bomb,
                "explosion_remaining": sample.explosion_remaining,
                "defuse_remaining": sample.defuse_remaining,
                "pcd_phase": sample.pcd_phase,
                "bomb_player": sample.bomb_player,
                "observed_steamid": sample.observed_steamid,
                "observing_other": sample.observing_other,
            }
            self.gsi_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.gsi_fh.flush()
            if sample.bomb_state in ("defusing", "defused", "exploded") or \
                    sample.round_phase == "over":
                self.labels.append(rec)

    def close(self, extra: dict) -> None:
        self.wav.close()
        self.gsi_fh.close()
        meta = {
            "started": datetime.fromtimestamp(
                time.time() - (time.monotonic() - self.t0_mono)).isoformat(timespec="seconds"),
            "rate": self.rate,
            "channels": 1,
            "sampwidth": 2,
            "frames": self.wav.frames,
            "duration_sec": round(self.wav.frames / self.rate, 2),
            "gsi_packets": self.gsi_count,
            "observer_packets": self.observer_count,
            "label_events": len(self.labels),
            **extra,
        }
        (self.outdir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return meta


def main() -> int:
    ap = argparse.ArgumentParser(description="同步采集：音频 + GSI")
    ap.add_argument("--port", type=int, default=30077)
    ap.add_argument("--device", default=None, help="播放设备 id；默认用默认设备")
    ap.add_argument("--devices", action="store_true", help="列出播放设备后退出")
    ap.add_argument("--gain", type=float, default=1.0,
                    help="录音增益；采集阶段建议 1.0，缩放留给分析阶段")
    ap.add_argument("--seconds", type=float, default=None,
                    help="只采这么多秒后自动停止（自检用）")
    args = ap.parse_args()

    if args.devices:
        for d in list_render_devices():
            print(f"  {'*' if d.is_default else ' '} {d.name}\n      {d.device_id}")
        return 0

    SESSIONS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = SESSIONS / f"session_{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)

    cfg = AudioConfig()
    cfg.device_id = args.device
    cfg.gain = 1.0

    rate = ANALYSIS_RATE
    session = Session(outdir, rate)

    print("=" * 88)
    print(f"同步采集 -> {outdir}")
    print("=" * 88)

    cap = LoopbackCapture(cfg, session.on_pcm, rate=rate)
    cap.start()
    if cap.error:
        print(f"[X] {cap.error}")
        session.close({"error": cap.error})
        return 1
    print(f"音频: 回环采集中  设备采样率 {cap.sample_rate_seen}Hz / {cap.channels_seen}ch "
          f"-> 写入 {rate}Hz 单声道 16bit")
    print(f"      采集源: {cap.description}")
    for w in cap.warnings:
        print(f"      [!] {w}")

    recv = GsiReceiver("127.0.0.1", args.port, on_sample=session.on_sample)
    try:
        recv.start()
    except OSError as exc:
        print(f"[X] 无法监听 {args.port} —— {exc}")
        cap.stop()
        session.close({"error": f"gsi port: {exc}"})
        return 1
    print(f"GSI : 监听 http://127.0.0.1:{args.port}/")
    print()
    print("请这样打一局（每一条都重要）：")
    print("  1) 活着安放一次炸弹")
    print("  2) 故意送死 -> 死后观战那个拆弹的 CT（这一步才能拿到 defusing 的精确标注）")
    print("  3) 让拆弹走完（成功或爆炸）")
    print("  4) 多打几回合，正常开枪走动（这段是用来量误报的）")
    print("  5) 打完一局后回到这里按 Ctrl+C")
    print("=" * 88)

    last_report = time.monotonic()
    last_print = last_report
    deadline = (time.monotonic() + args.seconds) if args.seconds else None
    try:
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                break
            if now - last_report >= 5.0:
                last_report = now
                session.wav.patch_header()
            if now - last_print >= 10.0:
                last_print = now
                dur = session.wav.frames / rate
                age = recv.age(now)
                age_s = "--" if age is None else f"{age:.1f}s"
                print(f"  [{dur:7.1f}s] GSI {session.gsi_count} 包 "
                      f"(观察者 {session.observer_count})  距上次 {age_s}  "
                      f"音频块 {cap.blocks}  标注事件 {len(session.labels)}", flush=True)
                if cap.error:
                    print(f"  [!] 音频异常: {cap.error}")
    except KeyboardInterrupt:
        pass
    finally:
        recv.stop()
        cap.stop()
        meta = session.close({
            "device_id": args.device,
            "device_rate": cap.sample_rate_seen,
            "device_channels": cap.channels_seen,
            "capture_source": cap.description,
            "capture_warnings": cap.warnings,
            "audio_error": cap.error,
            "gsi_dropped": recv.dropped,
            "gsi_parse_errors": recv.parse_errors,
        })

    print("\n" + "=" * 88)
    print("采集完成")
    print("=" * 88)
    for k in ("duration_sec", "gsi_packets", "observer_packets", "label_events"):
        print(f"  {k:<18} {meta.get(k)}")
    print(f"\n目录: {outdir}")
    print("把这个目录路径告诉我，我来做标定。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
