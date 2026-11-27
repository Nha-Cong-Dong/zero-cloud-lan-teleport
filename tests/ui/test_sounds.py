"""Kiểm thử hệ thống âm thanh tương tác (`teleport.ui.sounds`, cài đặt `sound_mode`/`sound_volume`)."""

from __future__ import annotations

import io
import struct
import wave

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


def test_synthesize_wav_valid_pcm_for_all_cues():
    from teleport.ui import sounds

    for cue in sounds.SoundCue:
        raw = sounds.synthesize_wav(cue, 70)
        assert raw[:4] == b"RIFF"
        assert raw[8:12] == b"WAVE"
        with wave.open(io.BytesIO(raw), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == sounds.SAMPLE_RATE
            frames = wf.readframes(wf.getnframes())
            assert len(frames) >= 100
            samples = struct.unpack(f"<{len(frames) // 2}h", frames)
            assert max(abs(s) for s in samples) > 0
            assert max(abs(s) for s in samples) <= 32767


def test_play_respects_mode_visibility_dnd_and_cooldown(monkeypatch):
    from teleport.ui import sounds

    sounds._force_silent(False)
    monkeypatch.setattr(sounds, "_qt_platform_silent", lambda: False)
    monkeypatch.setattr(sounds, "_is_dnd_active", lambda: False)
    clock = [10.0]
    monkeypatch.setattr(sounds, "_monotonic", lambda: clock[0])
    played: list[tuple[sounds.SoundCue, int]] = []
    monkeypatch.setattr(sounds, "_play_backend", lambda cue, wav: played.append((cue, len(wav))))

    # Mặc định FULL: tactile phát khi cửa sổ đang hiện, chặn khi ẩn
    assert sounds.play(sounds.SoundCue.TOGGLE_TICK, window_visible=False) is False
    assert sounds.play(sounds.SoundCue.TOGGLE_TICK, window_visible=True) is True
    # Cooldown chặn gọi dồn
    assert sounds.play(sounds.SoundCue.TOGGLE_TICK, window_visible=True) is False
    clock[0] += 1.0
    assert sounds.play(sounds.SoundCue.TOGGLE_TICK, window_visible=True) is True

    # Chế độ NOTIFY: chặn tactile, vẫn cho notify dù cửa sổ ẩn
    sounds.set_preference(sounds.SoundMode.NOTIFY, 80)
    clock[0] += 2.0
    assert sounds.play(sounds.SoundCue.DROP_LOCK, window_visible=True) is False
    assert sounds.play(sounds.SoundCue.OFFER_INCOMING, window_visible=False) is True

    # Chế độ OFF: chỉ force_preview=True mới phát
    sounds.set_preference(sounds.SoundMode.OFF, 80)
    clock[0] += 2.0
    assert sounds.play(sounds.SoundCue.OFFER_INCOMING) is False
    assert sounds.play(sounds.SoundCue.TRANSFER_DONE, force_preview=True) is True

    # DND hệ thống bật: chặn phát thường, vẫn cho preview
    sounds.set_preference(sounds.SoundMode.FULL, 70)
    monkeypatch.setattr(sounds, "_is_dnd_active", lambda: True)
    clock[0] += 2.0
    assert sounds.play(sounds.SoundCue.PAIR_SUCCESS) is False
    assert sounds.play(sounds.SoundCue.TRANSFER_DONE, force_preview=True) is True


def test_appearance_section_sound_controls_and_preview(qtbot, monkeypatch):
    from teleport.ui import sounds
    from teleport.ui.appearance import AppearanceSection

    sec = AppearanceSection()
    qtbot.addWidget(sec)
    sec.set_loaded(True)
    sec.set_sound_mode(None)
    sec.set_sound_volume(None)

    assert sec.sound_mode_combo.currentData() == "full"
    assert sec.sound_volume_slider.value() == sounds.DEFAULT_VOLUME
    assert sec.sound_volume_label.text() == "70%"
    assert sec.sound_volume_row.isEnabled()

    modes: list[object] = []
    volumes: list[object] = []
    sec.sound_mode_selected.connect(modes.append)
    sec.sound_volume_selected.connect(volumes.append)

    idx_notify = sec.sound_mode_combo.findData("notify")
    sec.sound_mode_combo.setCurrentIndex(idx_notify)
    sec.sound_mode_combo.activated.emit(idx_notify)
    assert modes == ["notify"]
    assert sec.sound_volume_row.isEnabled()

    idx_off = sec.sound_mode_combo.findData("off")
    sec.sound_mode_combo.setCurrentIndex(idx_off)
    sec.sound_mode_combo.activated.emit(idx_off)
    assert modes == ["notify", "off"]
    assert not sec.sound_volume_row.isEnabled()

    idx_full = sec.sound_mode_combo.findData("full")
    sec.sound_mode_combo.setCurrentIndex(idx_full)
    sec.sound_mode_combo.activated.emit(idx_full)
    assert modes == ["notify", "off", None]
    assert sec.sound_volume_row.isEnabled()

    sec.sound_volume_slider.setValue(45)
    sec.sound_volume_slider.sliderReleased.emit()
    assert volumes == [45]
    assert sec.sound_volume_label.text() == "45%"

    previewed: list[tuple[sounds.SoundCue, bool]] = []
    monkeypatch.setattr(
        sounds,
        "play",
        lambda cue, **kw: previewed.append((cue, bool(kw.get("force_preview")))) or True,
    )
    sec.sound_preview_button.click()
    QApplication.processEvents()
    assert previewed == [(sounds.SoundCue.TRANSFER_DONE, True)]
    assert sec.sound_preview_button.focusPolicy() != Qt.FocusPolicy.NoFocus


def test_settings_roundtrip_sound_and_onboarding(tmp_path):
    from teleport.app.settings import Settings

    s = Settings(sound_mode="notify", sound_volume=40, onboarding_dismissed=True)
    s.save(tmp_path)
    loaded = Settings.load(tmp_path)
    assert loaded.sound_mode == "notify"
    assert loaded.sound_volume == 40
    assert loaded.onboarding_dismissed is True

    # Giá trị mặc định không ghi ra đĩa
    Settings().save(tmp_path)
    loaded_default = Settings.load(tmp_path)
    assert loaded_default.sound_mode is None
    assert loaded_default.sound_volume is None
    assert loaded_default.onboarding_dismissed is False
