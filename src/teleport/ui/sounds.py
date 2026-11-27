"""Hệ thống âm thanh tương tác cho giao diện (v1.8.0): tổng hợp PCM trong RAM và phát bất đồng bộ.

Không dùng `QtMultimedia` hay thư viện âm thanh ngoài (`PySide6-Essentials` chỉ có nhân Qt và SVG),
nên toàn bộ 10 tín hiệu âm thanh được tổng hợp sẵn trong RAM dưới dạng WAV 16-bit 44,1 kHz (mono)
với thang âm trưởng ngũ cung ấm (quãng C5–C6), bao ADSR mượt và fade-in cosine >= 4 ms để không nổ
"tách".

**Hai nhóm tín hiệu** (`SoundCue`):
- Nhóm thông báo (`NOTIFY_CUES`): phát ở `SoundMode.NOTIFY` và `SoundMode.FULL`, được phép phát cả
  khi cửa sổ chính đang ẩn ở khay hệ thống (`OFFER_INCOMING`, `PAIR_SUCCESS`, `TRANSFER_DONE`,
  `GUEST_CONNECTED`, `ERROR_SOFT`).
- Nhóm tương tác (`TACTILE_CUES`): chỉ phát ở `SoundMode.FULL` và chỉ khi cửa sổ đang hiện, không
  thu nhỏ (`RADAR_PING`, `DEVICE_FOUND`, `DROP_LOCK`, `CLIPBOARD_SYNC`, `TOGGLE_TICK`).

**Chống làm phiền & An toàn**:
- Tôn trọng trạng thái Không làm phiền / Toàn màn hình trên Windows (`SHQueryUserNotificationState`).
- Cooldown riêng theo từng `SoundCue` để tránh dội âm khi nhiều sự kiện đến dồn dập.
- Bỏ qua khi nền tảng Qt là `offscreen` / `minimal` hoặc seam test `_silent` đang bật.
- Backend phát: Windows dùng `winsound.PlaySound(..., SND_MEMORY | SND_ASYNC | SND_NODEFAULT)` từ RAM;
  macOS dùng `afplay` với tệp tạm trong thư mục phiên; Linux tự dò `pw-play` -> `paplay` -> `aplay`.

Chỉ dùng thư viện chuẩn Python và `QGuiApplication` của PySide6; không import tầng trên.
"""

from __future__ import annotations

import atexit
import contextlib
import ctypes
import io
import logging
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtGui import QGuiApplication

log = logging.getLogger(__name__)

SAMPLE_RATE = 44_100
DEFAULT_VOLUME = 70  # 0..100
MIN_VOLUME = 0
MAX_VOLUME = 100
_VOLUME_BUCKET = 5  # làm tròn bước 5% để cache gọn trong RAM
_PLATFORMS_SILENT = frozenset({"offscreen", "minimal"})


class SoundMode(StrEnum):
    """Mức âm thanh người dùng chọn trong Cài đặt › Giao diện."""

    OFF = "off"
    NOTIFY = "notify"
    FULL = "full"


DEFAULT_MODE = SoundMode.FULL


class SoundCue(StrEnum):
    """10 tín hiệu âm thanh của ZCL Teleport."""

    # Nhóm thông báo (chạy cả khi ẩn khay nếu mode != OFF)
    OFFER_INCOMING = "offer_incoming"
    PAIR_SUCCESS = "pair_success"
    TRANSFER_DONE = "transfer_done"
    GUEST_CONNECTED = "guest_connected"
    ERROR_SOFT = "error_soft"

    # Nhóm tương tác (chỉ chạy ở mode FULL và khi cửa sổ đang hiện)
    RADAR_PING = "radar_ping"
    DEVICE_FOUND = "device_found"
    DROP_LOCK = "drop_lock"
    CLIPBOARD_SYNC = "clipboard_sync"
    TOGGLE_TICK = "toggle_tick"


NOTIFY_CUES: frozenset[SoundCue] = frozenset(
    {
        SoundCue.OFFER_INCOMING,
        SoundCue.PAIR_SUCCESS,
        SoundCue.TRANSFER_DONE,
        SoundCue.GUEST_CONNECTED,
        SoundCue.ERROR_SOFT,
    }
)
TACTILE_CUES: frozenset[SoundCue] = frozenset(
    {
        SoundCue.RADAR_PING,
        SoundCue.DEVICE_FOUND,
        SoundCue.DROP_LOCK,
        SoundCue.CLIPBOARD_SYNC,
        SoundCue.TOGGLE_TICK,
    }
)

_COOLDOWNS: dict[SoundCue, float] = {
    SoundCue.OFFER_INCOMING: 0.6,
    SoundCue.PAIR_SUCCESS: 0.5,
    SoundCue.TRANSFER_DONE: 0.8,
    SoundCue.GUEST_CONNECTED: 0.8,
    SoundCue.ERROR_SOFT: 0.5,
    SoundCue.RADAR_PING: 0.4,
    SoundCue.DEVICE_FOUND: 1.5,
    SoundCue.DROP_LOCK: 0.25,
    SoundCue.CLIPBOARD_SYNC: 0.35,
    SoundCue.TOGGLE_TICK: 0.08,
}

# Đặc tả nốt: (start_sec, duration_sec, freq_hz, rel_amp, harmonic2, harmonic3)
_NoteSpec = tuple[float, float, float, float, float, float]

_RECIPES: dict[SoundCue, tuple[float, tuple[_NoteSpec, ...]]] = {
    SoundCue.OFFER_INCOMING: (
        0.24,
        (
            (0.000, 0.120, 783.99, 0.85, 0.18, 0.05),  # G5
            (0.090, 0.150, 1046.50, 0.95, 0.15, 0.04),  # C6
        ),
    ),
    SoundCue.PAIR_SUCCESS: (
        0.34,
        (
            (0.000, 0.150, 523.25, 0.80, 0.20, 0.05),  # C5
            (0.070, 0.170, 659.25, 0.85, 0.18, 0.04),  # E5
            (0.140, 0.200, 783.99, 0.95, 0.16, 0.04),  # G5
        ),
    ),
    SoundCue.TRANSFER_DONE: (
        0.31,
        (
            (0.000, 0.140, 659.25, 0.80, 0.16, 0.04),  # E5
            (0.085, 0.220, 987.77, 0.92, 0.14, 0.03),  # B5
        ),
    ),
    SoundCue.GUEST_CONNECTED: (
        0.18,
        (
            (0.000, 0.090, 587.33, 0.78, 0.16, 0.03),  # D5
            (0.065, 0.115, 880.00, 0.85, 0.14, 0.02),  # A5
        ),
    ),
    SoundCue.ERROR_SOFT: (
        0.20,
        (
            (0.000, 0.095, 293.66, 0.85, 0.22, 0.06),  # D4
            (0.085, 0.115, 220.00, 0.80, 0.24, 0.06),  # A3
        ),
    ),
    SoundCue.RADAR_PING: (
        0.13,
        ((0.000, 0.130, 880.00, 0.55, 0.12, 0.00),),  # A5 sonar
    ),
    SoundCue.DEVICE_FOUND: (
        0.07,
        ((0.000, 0.070, 659.25, 0.50, 0.14, 0.00),),  # E5 pop
    ),
    SoundCue.DROP_LOCK: (
        0.06,
        ((0.000, 0.060, 523.25, 0.52, 0.18, 0.00),),  # C5 lock
    ),
    SoundCue.CLIPBOARD_SYNC: (
        0.085,
        (
            (0.000, 0.038, 783.99, 0.45, 0.10, 0.00),
            (0.038, 0.045, 1046.50, 0.48, 0.10, 0.00),
        ),
    ),
    SoundCue.TOGGLE_TICK: (
        0.04,
        ((0.000, 0.040, 698.46, 0.36, 0.10, 0.00),),  # F5 siêu nhẹ
    ),
}


# ---- Trạng thái cấu hình -------------------------------------------------------------------------

_mode: SoundMode = DEFAULT_MODE
_volume: int = DEFAULT_VOLUME
_silent: bool = False
_last_played: dict[SoundCue, float] = {}
_wav_cache: dict[tuple[SoundCue, int], bytes] = {}
_linux_player: str | bool | None = False  # False = chưa dò
_temp_dir: Path | None = None
_active_proc: subprocess.Popen[bytes] | None = None

_monotonic: Callable[[], float] = time.monotonic


def mode_from_setting(value: str | None) -> SoundMode:
    """Đổi giá trị lưu trong `settings.json` sang `SoundMode`; None hoặc lạ → `DEFAULT_MODE`."""
    if value is None:
        return DEFAULT_MODE
    try:
        return SoundMode(value)
    except ValueError:
        return DEFAULT_MODE


def volume_from_setting(value: int | None) -> int:
    """Đổi giá trị lưu sang int [0..100]; None hoặc ngoài khoảng → `DEFAULT_VOLUME`."""
    if value is None or type(value) is not int:
        return DEFAULT_VOLUME
    if not MIN_VOLUME <= value <= MAX_VOLUME:
        return DEFAULT_VOLUME
    return value


def current_mode() -> SoundMode:
    return _mode


def current_volume() -> int:
    return _volume


def set_preference(mode: SoundMode | str | None, volume: int | None = None) -> None:
    """Cập nhật chế độ và âm lượng hiện hành của module."""
    global _mode, _volume
    _mode = mode if isinstance(mode, SoundMode) else mode_from_setting(mode)
    if volume is not None:
        _volume = max(MIN_VOLUME, min(MAX_VOLUME, int(volume)))


def _force_silent(on: bool) -> None:
    """Seam test: chặn mọi lời gọi phát âm thật."""
    global _silent
    _silent = bool(on)


def _reset_for_tests() -> None:
    """Seam test: trả cấu hình về mặc định và xóa bộ nhớ chống dội."""
    global _mode, _volume, _silent
    _mode = DEFAULT_MODE
    _volume = DEFAULT_VOLUME
    _silent = False
    _last_played.clear()


# ---- Tổng hợp WAV --------------------------------------------------------------------------------


def _quantize_volume(volume: int) -> int:
    clamped = max(MIN_VOLUME, min(MAX_VOLUME, int(volume)))
    if clamped == 0:
        return 0
    return max(_VOLUME_BUCKET, round(clamped / _VOLUME_BUCKET) * _VOLUME_BUCKET)


def synthesize_wav(cue: SoundCue, volume: int = DEFAULT_VOLUME) -> bytes:
    """Tổng hợp dữ liệu WAV PCM 16-bit 44,1 kHz (mono) cho `cue` theo `volume` (0..100)."""
    cue = SoundCue(cue)
    bucket = _quantize_volume(volume)
    cached = _wav_cache.get((cue, bucket))
    if cached is not None:
        return cached
    total_sec, notes = _RECIPES[cue]
    total_samples = max(1, round(total_sec * SAMPLE_RATE))
    scale = (bucket / 100.0) * 0.45
    buf = [0.0] * total_samples
    attack = max(1, int(0.004 * SAMPLE_RATE))  # fade-in cosine >= 4 ms
    for start_sec, dur_sec, freq, rel_amp, h2, h3 in notes:
        i0 = max(0, round(start_sec * SAMPLE_RATE))
        length = max(attack + 1, round(dur_sec * SAMPLE_RATE))
        i1 = min(total_samples, i0 + length)
        decay_rate = 5.5 / max(dur_sec, 0.01)
        omega = 2.0 * math.pi * freq / SAMPLE_RATE
        for idx in range(i0, i1):
            local = idx - i0
            t = local / SAMPLE_RATE
            env = math.exp(-decay_rate * t)
            if local < attack:
                env *= 0.5 * (1.0 - math.cos(math.pi * local / attack))
            tail = i1 - idx
            if tail < attack:
                env *= 0.5 * (1.0 - math.cos(math.pi * tail / attack))
            phase = omega * local
            wave_val = math.sin(phase) + h2 * math.sin(2.0 * phase) + h3 * math.sin(3.0 * phase)
            buf[idx] += wave_val * rel_amp * env * scale
    peak = max((abs(s) for s in buf), default=0.0)
    norm = min(1.0, 0.92 / peak) if peak > 0.92 else 1.0
    pcm = struct.pack(
        f"<{total_samples}h",
        *(max(-32767, min(32767, round(s * norm * 32767.0))) for s in buf),
    )
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    result = out.getvalue()
    _wav_cache[(cue, bucket)] = result
    return result


# ---- Kiểm tra DND & Backend phát -----------------------------------------------------------------


def _query_windows_dnd() -> bool:
    """True nếu Windows đang ở chế độ Không làm phiền / Toàn màn hình (`SHQueryUserNotificationState`)."""
    if sys.platform != "win32":
        return False
    shell32 = ctypes.WinDLL("shell32")
    state = ctypes.c_int(0)
    hr = int(shell32.SHQueryUserNotificationState(ctypes.byref(state)))
    if hr < 0:
        return False
    # 2=QUNS_BUSY, 3=QUNS_RUNNING_D3D_FULL_SCREEN, 4=QUNS_PRESENTATION_MODE, 7=QUNS_QUIET_TIME
    return state.value in (2, 3, 4, 7)


def _default_is_dnd_active() -> bool:
    try:
        return _query_windows_dnd()
    except Exception:
        return False


_is_dnd_active: Callable[[], bool] = _default_is_dnd_active


def _cleanup_temp_dir() -> None:
    global _temp_dir, _active_proc
    proc, _active_proc = _active_proc, None
    if proc is not None:
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.terminate()
    d, _temp_dir = _temp_dir, None
    if d is not None:
        with contextlib.suppress(OSError):
            shutil.rmtree(d, ignore_errors=True)


def _session_temp_dir() -> Path:
    global _temp_dir
    if _temp_dir is None:
        _temp_dir = Path(tempfile.mkdtemp(prefix="teleport-sfx-"))
        atexit.register(_cleanup_temp_dir)
    return _temp_dir


def _find_linux_player() -> str | None:
    global _linux_player
    if _linux_player is False:
        found: str | None = None
        for cmd in ("pw-play", "paplay", "aplay"):
            path = shutil.which(cmd)
            if path is not None:
                found = path
                break
        _linux_player = found
    return _linux_player if isinstance(_linux_player, str) else None


def _default_play_backend(cue: SoundCue, wav_bytes: bytes) -> None:
    global _active_proc
    if sys.platform == "win32":
        import winsound

        flags = winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        winsound.PlaySound(wav_bytes, flags)
        return
    if sys.platform == "darwin":
        cmd = "/usr/bin/afplay"
    else:
        cmd = _find_linux_player() or ""
    if not cmd:
        return
    wav_path = _session_temp_dir() / f"{cue.value}.wav"
    if not wav_path.exists() or wav_path.stat().st_size != len(wav_bytes):
        wav_path.write_bytes(wav_bytes)
        with contextlib.suppress(OSError):
            wav_path.chmod(0o600)
    prev = _active_proc
    if prev is not None and prev.poll() is None:
        with contextlib.suppress(Exception):
            prev.terminate()
    _active_proc = subprocess.Popen(
        [cmd, str(wav_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


_play_backend: Callable[[SoundCue, bytes], Any] = _default_play_backend


# ---- Cổng phát công khai -------------------------------------------------------------------------


def _qt_platform_silent() -> bool:
    try:
        app = QGuiApplication.instance()
        if app is None:
            return False
        return QGuiApplication.platformName() in _PLATFORMS_SILENT
    except Exception:
        return False


def play(
    cue: SoundCue,
    *,
    force_preview: bool = False,
    window_visible: bool = True,
) -> bool:
    """Phát tín hiệu `cue` nếu mọi điều kiện cho phép; trả True nếu đã gửi tới backend phát."""
    cue = SoundCue(cue)
    if _silent or _qt_platform_silent():
        return False
    if not force_preview:
        if _mode is SoundMode.OFF or _volume <= 0:
            return False
        if cue in TACTILE_CUES and (_mode is not SoundMode.FULL or not window_visible):
            return False
        if _is_dnd_active():
            return False
        now = _monotonic()
        cooldown = _COOLDOWNS.get(cue, 0.35)
        last = _last_played.get(cue)
        if last is not None and now - last < cooldown:
            return False
        _last_played[cue] = now
    effective_volume = _volume if _volume > 0 else DEFAULT_VOLUME
    try:
        wav = synthesize_wav(cue, effective_volume)
        _play_backend(cue, wav)
    except Exception:
        log.debug("không phát được âm thanh %s", cue.value, exc_info=True)
        return False
    return True
