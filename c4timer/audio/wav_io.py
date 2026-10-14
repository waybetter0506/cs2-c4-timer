"""WAV reading/writing and channel/rate conversion.

Recording captures at the endpoint's native mix format (48kHz stereo float32),
while analysis works at a fixed mono rate, so something has to bridge the two.
This module is pure stdlib plus numpy - no audio framework needed.
"""
from __future__ import annotations

import struct
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# WAVE format tags we accept.
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


@dataclass
class Audio:
    """Mono float32 samples plus the rate they were captured at."""

    samples: np.ndarray
    rate: int
    channels: int = 1

    @property
    def duration(self) -> float:
        return float(self.samples.size) / self.rate

    def resample(self, target_rate: int) -> "Audio":
        if target_rate == self.rate:
            return self
        return Audio(_resample(self.samples, self.rate, target_rate), target_rate, 1)


def _decode_pcm(raw: bytes, sample_width: int, channels: int) -> np.ndarray:
    """Decode interleaved PCM bytes to float32 in [-1, 1]."""
    if sample_width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 1:
        # 8-bit WAV is unsigned.
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        val = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        val = np.where(val & 0x800000, val - 0x1000000, val)
        data = val.astype(np.float32) / 8388608.0
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {sample_width}")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32)


def read_wav(path: str | Path, target_rate: int | None = None) -> Audio:
    """Read a WAV and return mono float32, optionally resampled.

    The RIFF header is parsed directly rather than through the ``wave`` module:
    ``wave`` rejects WAVE_FORMAT_IEEE_FLOAT outright, and files coming from
    Windows audio tools can also carry a zero ``cbSize`` on an extensible
    header, which the stdlib treats as fatal. Neither is worth losing a
    recording over.
    """
    path = Path(path)
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"not a RIFF/WAVE file: {path}")

    fmt_tag = 0
    channels = rate = width = 0
    payload = b""
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8: pos + 8 + size]
        if chunk_id == b"fmt " and len(body) >= 16:
            fmt_tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from(
                "<HHIIHH", body, 0)
            width = bits // 8
            if fmt_tag == WAVE_FORMAT_EXTENSIBLE and len(body) >= 40:
                # The real format lives in the SubFormat GUID's first two bytes.
                sub = struct.unpack_from("<H", body, 24)[0]
                if sub in (WAVE_FORMAT_PCM, WAVE_FORMAT_IEEE_FLOAT):
                    fmt_tag = sub
        elif chunk_id == b"data":
            payload = body
        pos += 8 + size + (size & 1)

    if width <= 0 or channels <= 0:
        raise ValueError(f"missing or malformed fmt chunk in {path}")

    if fmt_tag == WAVE_FORMAT_IEEE_FLOAT and width == 4:
        samples = np.frombuffer(payload, dtype="<f4").astype(np.float32)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        samples = np.ascontiguousarray(samples, dtype=np.float32)
    elif fmt_tag == WAVE_FORMAT_PCM:
        samples = _decode_pcm(payload, width, channels)
    else:
        raise ValueError(f"unsupported WAVE format tag {fmt_tag} (width {width})")

    audio = Audio(samples, rate, channels)
    if target_rate is not None:
        audio = audio.resample(target_rate)
    return audio


def write_wav(path: str | Path, audio: Audio, sample_width: int = 4) -> Path:
    """Write mono audio as a WAV file (float32 by default).

    The RIFF header is built by hand: Python's ``wave`` module cannot emit
    WAVE_FORMAT_IEEE_FLOAT, and writing float samples under the PCM tag (which
    is what ``wave`` would do) produces a file that other tools will happily
    read as garbage.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.asarray(audio.samples, dtype=np.float32).ravel()

    if sample_width == 4:
        fmt_tag, bits, payload = WAVE_FORMAT_IEEE_FLOAT, 32, samples.astype("<f4").tobytes()
    elif sample_width == 2:
        fmt_tag, bits = WAVE_FORMAT_PCM, 16
        payload = np.clip(samples * 32767.0, -32768, 32767).astype("<i2").tobytes()
    else:
        raise ValueError("sample_width must be 2 or 4")

    channels = 1
    block_align = channels * (bits // 8)
    fmt_chunk = struct.pack(
        "<HHIIHH", fmt_tag, channels, audio.rate,
        audio.rate * block_align, block_align, bits,
    )
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
    body += b"data" + struct.pack("<I", len(payload)) + payload
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def _resample(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolation resampling.

    Adequate for a first pass; template matching is tolerant of the mild
    high-frequency roll-off this introduces, but if calibration shows it
    matters we should move to a polyphase filter.
    """
    x = np.asarray(x, dtype=np.float32).ravel()
    if x.size == 0 or src_rate == dst_rate:
        return x
    n_out = int(round(x.size * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    src_pos = np.arange(n_out, dtype=np.float64) * (src_rate / dst_rate)
    return np.interp(src_pos, np.arange(x.size), x).astype(np.float32)


def concatenate(parts: list[tuple[Audio, float]], total: float | None = None,
                rate: int = 48_000) -> Audio:
    """Place each clip at its timestamp on a silent timeline.

    ``parts`` is a list of ``(audio, start_seconds)``.
    """
    ends = [start + part.duration for part, start in parts]
    length = total if total is not None else (max(ends) if ends else 0.0)
    n = int(round(length * rate))
    out = np.zeros(n, dtype=np.float32)
    for part, start in parts:
        at = part.resample(rate) if part.rate != rate else part
        i = int(round(start * rate))
        if i >= n:
            continue
        seg = at.samples[: max(0, n - i)]
        out[i:i + seg.size] += seg
    return Audio(out, rate, 1)


def noise_burst(rate: int, duration_ms: float, seed: int, band: tuple[float, float] | None = None,
                decay: float = 6.0) -> Audio:
    """A percussive noise burst.

    Deliberately broadband rather than a tone: a pure tone correlates with
    itself at every period and produces a comb of false peaks, which is a
    property of the test signal, not of the detector.
    """
    n = int(rate * duration_ms / 1000.0)
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, 1.0, n)
    if band is not None:
        lo, hi = band
        spec = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(n, 1.0 / rate)
        spec[(freqs < lo) | (freqs > hi)] = 0.0
        x = np.fft.irfft(spec, n)
    # Percussive envelope: near-instant attack, exponential decay.
    t = np.arange(n) / rate
    env = np.exp(-decay * t)
    attack = max(1, int(rate * 0.002))
    env[:attack] *= np.linspace(0.0, 1.0, attack)
    x = x * env
    peak = float(np.max(np.abs(x))) or 1.0
    return Audio((x / peak * 0.9).astype(np.float32), rate, 1)
