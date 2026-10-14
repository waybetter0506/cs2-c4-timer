"""后台采集线程：WASAPI 回环 -> 单声道 float32 -> 回调。

已知限制（2 代已实测确认，不是推测）：
本机是 Windows 10 build 19041，而 `ActivateAudioInterfaceAsync` 的
`AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`（只录 CS2 一路音频）需要 build 20348+，
在本机返回 E_INVALIDARG。所以只能用**整机回环** —— 所有应用的声音都会被录进来。
缓解手段不是靠这里，而是靠"GSI 说已安放之后才允许音频触发拆弹"这条铁律。
"""

from __future__ import annotations

import threading
from typing import Callable

import numpy as np

from ..config import AudioConfig
from .detect import ANALYSIS_RATE
from .loopback import open_capture
from .wav_io import _resample


def to_mono_float32(raw: bytes, fmt) -> np.ndarray:
    """WASAPI 给的原始交错字节 -> 单声道 float32。"""
    width = getattr(fmt, "bytes_per_sample", 4)
    if width == 4:
        data = np.frombuffer(raw, dtype=np.float32)
    elif width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        raise ValueError(f"不支持的采样位宽: {width * 8} bit")
    channels = max(1, int(getattr(fmt, "channels", 1)))
    if channels > 1:
        usable = (data.size // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32)


class LoopbackCapture:
    """把回环音频按块喂给 `on_pcm`。线程出错不抛给调用方，记在 `error` 里。"""

    def __init__(
        self,
        cfg: AudioConfig,
        on_pcm: Callable[[np.ndarray], None],
        rate: int = ANALYSIS_RATE,
    ) -> None:
        self.cfg = cfg
        self.on_pcm = on_pcm
        self.rate = rate
        self.error: str | None = None
        self.opened = False
        self.description: str | None = None
        self.warnings: list[str] = []
        self.sample_rate_seen: int | None = None
        self.channels_seen: int | None = None
        self.blocks = 0
        self._client = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            mode = "auto"
            process_names = ("cs2.exe",)
            # open_capture 返回 (client, description, warnings)：
            # 进程级回环在本机（Win10 19041）必然失败并降级为整机回环，
            # 那条 warning 必须让上层看到 —— 否则用户不会知道别人说话/放歌也会被录进来。
            client, description, warnings = open_capture(
                mode=mode,
                device_id=self.cfg.device_id,
                buffer_ms=self.cfg.buffer_ms,
                process_names=process_names,
            )
        except Exception as exc:  # 采集开不起来不该让整个程序崩
            self.error = f"打开音频回环失败: {exc!r}"
            return

        self._client = client
        self.opened = True
        self.description = description
        self.warnings = list(warnings or [])
        try:
            self.sample_rate_seen = client.fmt.sample_rate
            self.channels_seen = client.fmt.channels
        except Exception as exc:
            self.error = f"读取音频格式失败: {exc!r}"
            return
        try:
            client.start()
        except Exception as exc:
            self.error = f"启动采集失败: {exc!r}"
            return

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audio-cap", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        client, self._client = self._client, None
        if client is not None:
            try:
                client.stop()
                client.close()
            except Exception:
                pass

    # ------------------------------------------------------------------

    def _run(self) -> None:
        client = self._client
        if client is None:
            return
        fmt = client.fmt
        gain = float(self.cfg.gain)
        needs_resample = fmt.sample_rate != self.rate
        try:
            while not self._stop.is_set():
                raw = client.read_chunk(timeout_ms=self.cfg.read_timeout_ms)
                if not raw:
                    continue
                mono = to_mono_float32(raw, fmt)
                if mono.size == 0:
                    continue
                if needs_resample:
                    mono = _resample(mono, fmt.sample_rate, self.rate)
                if gain != 1.0:
                    mono = mono * gain
                self.blocks += 1
                try:
                    self.on_pcm(mono)
                except Exception as exc:  # 消费端出错也不能让采集线程死掉
                    self.error = f"音频消费端异常: {exc!r}"
        except Exception as exc:
            self.error = f"采集线程异常: {exc!r}"

    def __enter__(self) -> "LoopbackCapture":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
