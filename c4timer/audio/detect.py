"""Streaming audio event detector.

Pipeline (the cascade described in STATUS.md):

    10ms PCM frames
      -> short-time RMS envelope          (level meter + cheap gate)
      -> normalized cross-correlation     (template match, level-invariant)
      -> onset back-dating                (report the attack, not the trigger)
      -> per-class peak picking + refractory suppression
      -> timestamped Detection objects

The point of back-dating is accuracy: a detector only learns about a sound
*after* enough of it has arrived to match, so the naive timestamp is late by the
match length. We keep history and walk back to the envelope's sharp rise, which
hands those milliseconds back to the state machine.

Templates and thresholds are placeholders until real recordings arrive; the
matching itself is complete and verified against synthetic signals.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import ANALYSIS_RATE
from .events import Detection, Event

# `ANALYSIS_RATE` 从包根导入（唯一定义处）；这里再导出一次，是为了让
# `from .detect import ANALYSIS_RATE` 这类老写法继续有效（capture.py / tools 都在用）。
HOP_MS = 5.0
ENV_WINDOW_MS = 5.0
DEFAULT_TEMPLATE_MS = 120.0
# Non-maximum suppression reach, in milliseconds. This must be far smaller than
# the template: a windowed match against a periodic sound puts legitimate
# maxima only a few samples either side of the true peak (the comb), and a
# suppression window as wide as the template would treat those comb teeth as
# rival sounds and reject the real peak. 10ms is wider than any comb spacing
# while still separating onsets that a human would call distinct.
NMS_WINDOW_MS = 10.0
# Correlation work is batched until this much new audio has arrived. Larger
# values mean fewer (and better amortised) FFTs at the cost of that much extra
# reporting latency on top of the template length. 40ms is imperceptible next
# to the countdowns we display.
SCORE_HOP_MS = 40.0


@dataclass
class Template:
    """A normalized waveform excerpt used as a matched filter."""

    event: Event
    samples: np.ndarray
    # None means "use the detector's default". A number is used verbatim - 0.0
    # is a legitimate (if reckless) threshold, so it must not be conflated with
    # "unset".
    threshold: float | None = None
    refractory_ms: float | None = None
    rate: int = ANALYSIS_RATE
    _norm: np.ndarray = field(default=None, repr=False)
    _norm_length: float = field(default=0.0, repr=False)
    # Cached transform of this template, keyed by FFT size. The template never
    # changes during a session, so recomputing its transform every frame is
    # pure waste - and it was the difference between keeping up and not.
    _kernel_fft: np.ndarray = field(default=None, repr=False)
    _kernel_fft_size: int = field(default=0, repr=False)

    def resampled(self, rate: int) -> "Template":
        """Same template for a different sample rate.

        Recordings and the capture device need not share a rate (Windows mix
        format is whatever the user configured), and a template used at the
        wrong rate simply never matches. Resampling here means a calibrated
        template set keeps working if the device's format changes.
        """
        if rate == self.rate:
            return self
        from .wav_io import _resample

        return Template(
            event=self.event,
            samples=_resample(self.samples, self.rate, rate),
            threshold=self.threshold,
            refractory_ms=self.refractory_ms,
            rate=rate,
        )

    def __post_init__(self) -> None:
        x = np.asarray(self.samples, dtype=np.float64).ravel()
        x = x - x.mean()
        n = float(np.linalg.norm(x))
        self._norm = x / n if n > 0 else x
        self._norm_length = float(np.linalg.norm(self._norm - self._norm.mean()))

    @property
    def length(self) -> int:
        return int(self._norm.size)

    def score(self, signal: np.ndarray) -> np.ndarray:
        """Normalized cross-correlation of ``signal`` against this template.

        Same result as :func:`normalized_xcorr`, but reuses the template's
        cached FFT.
        """
        t = self._norm
        s = np.asarray(signal, dtype=np.float64).ravel()
        n = s.size
        t_len = t.size
        if t_len == 0 or n < t_len:
            return np.zeros(0, dtype=np.float64)

        t_zm = t - t.mean()
        t_norm = self._norm_length
        if t_norm == 0:
            return np.zeros(n - t_len + 1, dtype=np.float64)

        win = n - t_len + 1
        csum = np.concatenate(([0.0], np.cumsum(s)))
        csum2 = np.concatenate(([0.0], np.cumsum(s * s)))
        win_sum = csum[t_len:] - csum[:-t_len]
        win_sum2 = csum2[t_len:] - csum2[:-t_len]
        var = win_sum2 - (win_sum * win_sum) / t_len
        win_norm = np.sqrt(np.maximum(var, 0.0))

        size = _fft_size(n, t_len)
        if self._kernel_fft_size != size or self._kernel_fft is None:
            self._kernel_fft = np.fft.rfft(t_zm[::-1], size)
            self._kernel_fft_size = size
        numer = _valid_correlate_fft(s, t_zm, (size, self._kernel_fft))

        denom = win_norm * t_norm
        out = np.zeros(win, dtype=np.float64)
        np.divide(numer, denom, out=out, where=denom > 1e-12)
        return out


class RingBuffer:
    """Fixed-capacity float32 history with absolute sample indexing."""

    def __init__(self, capacity: int):
        self.capacity = int(capacity)
        self.data = np.zeros(self.capacity, dtype=np.float32)
        self.total = 0  # samples ever written

    def write(self, block: np.ndarray) -> None:
        block = np.asarray(block, dtype=np.float32).ravel()
        n = block.size
        if n >= self.capacity:
            self.data[:] = block[-self.capacity:]
            self.total += n
            return
        end = self.total % self.capacity
        first = min(n, self.capacity - end)
        self.data[end:end + first] = block[:first]
        if n > first:
            self.data[:n - first] = block[first:]
        self.total += n

    def last(self, count: int) -> np.ndarray:
        """Most recent ``count`` samples, oldest first."""
        count = min(int(count), min(self.total, self.capacity))
        if count <= 0:
            return np.zeros(0, dtype=np.float32)
        end = self.total % self.capacity
        if end >= count:
            return self.data[end - count:end].copy()
        return np.concatenate((self.data[self.capacity - (count - end):], self.data[:end]))


def rms_envelope(x: np.ndarray, window: int, hop: int) -> np.ndarray:
    """Short-time RMS. Returns one value per hop."""
    if x.size < window:
        return np.zeros(0, dtype=np.float64)
    n_frames = 1 + (x.size - window) // hop
    idx = np.arange(window)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx].astype(np.float64)
    return np.sqrt(np.mean(frames * frames, axis=1))


def _fft_size(n: int, m: int) -> int:
    size = 1
    while size < n + m:
        size <<= 1
    return size


def _valid_correlate_fft(signal: np.ndarray, kernel: np.ndarray,
                         cached: tuple[int, np.ndarray] | None = None) -> np.ndarray:
    """Cross-correlation with the ``valid`` convention, via FFT.

    ``np.correlate`` is a direct O(n*m) sum and dominated the detector's runtime
    (94% in profiling). The FFT form is O(n log n) and agrees with it to within
    float rounding. ``cached`` optionally carries a previously computed
    ``(size, fft_of_reversed_kernel)``; the template is constant for a whole
    session, so recomputing its transform every frame is pure waste.
    """
    n, m = signal.size, kernel.size
    if m == 0 or n < m:
        return np.zeros(0, dtype=np.float64)
    size = _fft_size(n, m)
    if cached is not None and cached[0] == size:
        fk = cached[1]
    else:
        fk = np.fft.rfft(kernel[::-1], size)
    fs = np.fft.rfft(signal, size)
    conv = np.fft.irfft(fs * fk, size)
    # conv[k] = sum signal[i] * kernel[i - k + m - 1]; the valid region starts
    # at index m - 1.
    return conv[m - 1:n].copy()


def normalized_xcorr(signal: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Normalized cross-correlation in [0, 1], level-invariant.

    Equivalent to cosine similarity between the template and every window of the
    signal of the same length. The signal window's mean is removed as well as
    the template's, which is what makes the score independent of any DC offset
    in the recording.
    """
    t = np.asarray(template, dtype=np.float64).ravel()
    s = np.asarray(signal, dtype=np.float64).ravel()
    if t.size == 0 or s.size < t.size:
        return np.zeros(0, dtype=np.float64)

    n = s.size
    t_len = t.size
    t_zm = t - t.mean()
    t_norm = float(np.linalg.norm(t_zm))
    if t_norm == 0:
        return np.zeros(n - t_len + 1, dtype=np.float64)

    win = n - t_len + 1
    # Denominator: sliding window norm of the zero-mean signal.
    csum = np.concatenate(([0.0], np.cumsum(s)))
    csum2 = np.concatenate(([0.0], np.cumsum(s * s)))
    win_sum = csum[t_len:] - csum[:-t_len]
    win_sum2 = csum2[t_len:] - csum2[:-t_len]
    var = win_sum2 - (win_sum * win_sum) / t_len
    win_norm = np.sqrt(np.maximum(var, 0.0))

    # Numerator: correlation of the signal with the zero-mean template, with the
    # sliding window mean removed.
    numer = _valid_correlate_fft(s, t_zm)

    denom = win_norm * t_norm
    out = np.zeros(win, dtype=np.float64)
    np.divide(numer, denom, out=out, where=denom > 1e-12)
    return out


def find_peaks(scores: np.ndarray, threshold: float) -> np.ndarray:
    """Indices of local maxima at or above ``threshold``, left to right.

    A point qualifies when it is >= both neighbours and > at least one of them.
    The asymmetric ``> left, >= right`` form is tempting but silently drops the
    strongest peak whenever its two neighbours happen to be exactly equal,
    which is common for a perfectly centred match.
    """
    s = np.asarray(scores, dtype=np.float64).ravel()
    if s.size < 3:
        return np.flatnonzero(s >= threshold)
    mid, left, right = s[1:-1], s[:-2], s[2:]
    is_peak = (
        (mid >= threshold)
        & (mid >= left)
        & (mid >= right)
        & ((mid > left) | (mid > right))
    )
    return np.flatnonzero(is_peak) + 1


class Detector:
    """Feeds PCM to a bank of templates and emits Detections.

    Detection is *position-aware*: each class keeps a cursor at the sample up to
    which it has already reported an onset, and every new incoming frame only
    looks for peaks beyond it. Re-running the matcher over the whole history on
    every frame would report the same sound over and over, which is what the
    cursor exists to prevent.

    Reporting latency is bounded by the template length: a window cannot be
    matched until the template's worth of samples has arrived. The reported
    *time*, however, is back-dated to the attack, so the state machine still
    receives the true onset.
    """

    def __init__(self, sample_rate: int = ANALYSIS_RATE, history_ms: float = 1000.0,
                 templates: list[Template] | None = None, threshold: float = 0.75,
                 refractory_ms: float = 400.0):
        self.rate = int(sample_rate)
        self.history_ms = float(history_ms)
        self.default_threshold = float(threshold)
        self.default_refractory_ms = float(refractory_ms)
        self.templates: list[Template] = list(templates or [])
        capacity = int(self.rate * (history_ms / 1000.0))
        self.ring = RingBuffer(capacity)
        self.samples_seen = 0
        # Absolute sample from which each class should next look for an onset.
        self._cursor: dict[Event, int] = {}
        # Per-event rolling score array plus the absolute sample its element 0
        # refers to, and the last sample for which a score has been computed.
        self._scores: dict[Event, np.ndarray] = {}
        self._score_start: dict[Event, int] = {}
        self._score_end: dict[Event, int] = {}
        self._candidates: dict[Event, list[tuple[int, int, float]]] = {}
        self._pending: list[Detection] = []

    def reset(self) -> None:
        """Drop all detection state (e.g. after a device change)."""
        self._cursor.clear()
        self._scores.clear()
        self._score_start.clear()
        self._score_end.clear()
        self._candidates.clear()
        self._pending.clear()

    # -- configuration -----------------------------------------------------
    def add_template(self, template: Template) -> None:
        """Add a template, converting it to this detector's rate if needed."""
        self.templates.append(
            template if template.rate == self.rate else template.resampled(self.rate)
        )

    def load_templates(self, path) -> int:
        """Load templates from an .npz produced by the labeller.

        Arrays are stored under ``tmpl_<event>`` keys; an optional ``rate`` array
        records the rate they were captured at, so a set built on one machine
        still works on another whose mix format differs.
        """
        before = len(self.templates)
        with np.load(path, allow_pickle=False) as z:
            src_rate = int(z["rate"]) if "rate" in z.files else self.rate
            for key in z.files:
                if not key.startswith("tmpl_"):
                    continue
                event = Event(key[len("tmpl_"):])
                samples = np.asarray(z[key], dtype=np.float32).ravel()
                self.add_template(Template(event=event, samples=samples, rate=src_rate))
        return len(self.templates) - before

    # -- streaming ---------------------------------------------------------
    def feed(self, pcm: np.ndarray) -> list[Detection]:
        """Push float32 mono samples; returns events newly detected."""
        block = np.asarray(pcm, dtype=np.float32).ravel()
        if block.size == 0:
            return []
        self.ring.write(block)
        self.samples_seen += block.size
        found: list[Detection] = []
        for template in self.templates:
            found.extend(self._match_one(template))
            self._trim_scores(template.event)
        found.sort(key=lambda d: d.time)
        if found:
            self._pending.extend(found)
        return found

    def take_detections(self) -> list[Detection]:
        """Return and clear everything detected since the last call.

        Live code reads via this instead of the return value of :meth:`feed`,
        so a consumer that batches frames itself cannot silently drop events.
        """
        out = self._pending
        self._pending = []
        return out

    @property
    def now(self) -> float:
        return self.samples_seen / self.rate

    def _template_nsamples(self, template: Template) -> int:
        if template.length:
            return template.length
        return int(self.rate * (DEFAULT_TEMPLATE_MS / 1000.0))

    def _match_one(self, template: Template) -> list[Detection]:
        t_len = self._template_nsamples(template)
        if t_len <= 0 or self.ring.total < t_len:
            return []
        event = template.event

        self._extend_scores(event, template, t_len)
        arr = self._scores.get(event)
        if arr is None or arr.size == 0:
            return []
        base = self._score_start[event]
        threshold = template.threshold if template.threshold is not None else self.default_threshold
        refractory_ms = (
            template.refractory_ms if template.refractory_ms is not None
            else self.default_refractory_ms
        )
        refractory = int(self.rate * refractory_ms / 1000.0)
        consumed = self._cursor.get(event, 0)

        # -- non-maximum suppression, then emit -----------------------------
        # A candidate is only reported once its suppression shoulders have
        # arrived (so a stronger peak nearby cannot supersede it) and it beats
        # every other candidate within NMS_WINDOW_MS on both sides. The NMS
        # window must stay far smaller than the template: a windowed match
        # against a periodic sound puts legitimate maxima only a few samples
        # either side of the true peak, and a template-wide window would treat
        # those teeth as rival sounds and reject the real peak.
        nms = max(1, int(self.rate * NMS_WINDOW_MS / 1000.0))
        out: list[Detection] = []

        # Only scan the region that can still produce a new onset. Scanning the
        # whole retained array every frame is what makes long recordings crawl.
        lo = max(0, consumed - base - nms)
        hi = arr.size
        if hi <= lo:
            return []
        region = arr[lo:hi]

        for idx in find_peaks(region, threshold):
            i = lo + int(idx)
            match_abs = base + i
            if match_abs < consumed:
                continue
            if i - nms < 0 or i + max(nms, t_len) >= arr.size:
                continue
            peak = float(arr[i])
            if peak < float(arr[i - nms:i].max()) or peak < float(arr[i + 1:i + nms + 1].max()):
                continue

            onset_abs = self._backdate(match_abs, t_len)
            last = self._cursor.get(event)
            if last is not None and onset_abs - last < refractory:
                # A retrigger of the sound we just reported: consume it.
                self._cursor[event] = max(last, match_abs)
                continue
            self._cursor[event] = match_abs + t_len
            out.append(Detection(event=event, time=onset_abs / self.rate, confidence=peak))
        return out

    def _extend_scores(self, event: Event, template: Template, t_len: int) -> None:
        """Append the newly available correlation scores.

        Recomputing the correlation over the whole history on every frame is
        O(n^2) and stalls on multi-minute recordings, so only the samples that
        arrived since the last call are correlated. Two further economies keep
        the FFT count down: the template's transform is cached (it never
        changes), and work is batched until SCORE_HOP_MS of new audio has
        accumulated rather than running for every 10ms frame.
        """
        computed_to = self._score_end.get(event, 0)
        newest = self.samples_seen - t_len
        if newest <= computed_to:
            return
        if newest - computed_to < int(self.rate * SCORE_HOP_MS / 1000.0):
            return

        from_abs = max(0, computed_to)
        window = self.ring.last(self.samples_seen - from_abs)
        if window.size < t_len:
            return
        fresh = template.score(window)
        if fresh.size == 0:
            return

        if event in self._scores:
            prev_end = self._score_start[event] + self._scores[event].size
            # Drop the overlap we already stored, then append the rest.
            skip = max(0, prev_end - from_abs)
            if skip < fresh.size:
                self._scores[event] = np.concatenate((self._scores[event], fresh[skip:]))
        else:
            self._scores[event] = fresh
            self._score_start[event] = from_abs

        self._score_end[event] = from_abs + fresh.size - 1

    def _trim_scores(self, event: Event) -> None:
        """Bound the retained score history.

        The cut is driven by the newest computed score, not by the cursor: with
        no detections the cursor never moves, and a cursor-driven cut would let
        the array grow without limit for the whole recording (which is exactly
        what made long files crawl). Keeping ``history_ms`` plus a template
        length of context is more than enough to resolve any onset.
        """
        arr = self._scores.get(event)
        if arr is None:
            return
        margin = 2 * self._trim_margin(event)
        keep = max(margin, int(self.rate * self.history_ms / 1000.0))
        if arr.size <= keep:
            return
        drop = arr.size - keep
        self._scores[event] = arr[drop:]
        self._score_start[event] += drop
        # A trimmed score can no longer be reported; keep the cursor sane.
        cut = self._score_start[event]
        if self._cursor.get(event, 0) < cut:
            self._cursor[event] = cut

    def _trim_margin(self, event: Event) -> int:
        template = next((t for t in self.templates if t.event is event), None)
        return self._template_nsamples(template) if template else 0

    def _backdate(self, match_abs: int, t_len: int) -> int:
        """Walk back from a match to where the sound actually starts.

        The matched filter fires once a whole template has arrived, so its index
        is late by design and the reported onset has to be moved back. Two
        guards keep this from running away in noise:

        * the level to compare against is the peak *inside the match*, not the
          envelope at the match start - the match index sits on the rising edge,
          so using it under-references the level and the walk never stops;
        * the walk is capped at one template length.
        """
        max_lookback = min(t_len, int(self.rate * 0.060))
        start = max(0, match_abs - max_lookback)
        segment = self.ring.last(self.samples_seen - start)
        if segment.size == 0:
            return match_abs

        hop = max(1, int(self.rate * HOP_MS / 1000.0))
        env = rms_envelope(
            segment,
            window=max(1, int(self.rate * ENV_WINDOW_MS / 1000.0)),
            hop=hop,
        )
        if env.size < 3:
            return match_abs

        rel = match_abs - start
        peak_idx = min(env.size - 1, max(0, rel // hop))
        match_end_idx = min(env.size - 1, (rel + t_len) // hop)
        if match_end_idx >= peak_idx:
            level = float(env[peak_idx:match_end_idx + 1].max())
        else:
            level = float(env[peak_idx])
        if level <= 0.0:
            return match_abs

        floor = level * 0.25
        idx = peak_idx
        while idx > 0 and env[idx] > floor:
            idx -= 1
        # `idx` is the last frame at or below the floor; the sound begins at the
        # next hop boundary.
        onset_abs = start + (idx + 1) * hop
        return max(start, min(onset_abs, match_abs))


def synth_burst(rate: int, duration_ms: float, freq: float, attack_ms: float = 2.0,
                seed: int = 0) -> np.ndarray:
    """A test signal: a tone burst with a sharp attack. Used by the self-test."""
    n = int(rate * duration_ms / 1000.0)
    t = np.arange(n) / rate
    tone = np.sin(2 * np.pi * freq * t)
    env = np.ones(n)
    a = max(1, int(rate * attack_ms / 1000.0))
    env[:a] = np.linspace(0.0, 1.0, a)
    env[-max(1, a // 4):] = np.linspace(1.0, 0.0, max(1, a // 4))
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 0.01, n)
    return (tone * env + noise).astype(np.float32)
