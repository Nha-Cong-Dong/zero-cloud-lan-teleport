"""Kiểm thử sans-IO cho máy trạng thái cổng đẩy tự động (`teleport.core.watch`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from teleport.core.watch import (
    WatchPortalRule,
    WatchTracker,
    is_ignored_watch_name,
    watch_portal_from_dict,
    watch_portal_to_dict,
)

DEV_A = "a" * 32


def test_watch_rule_codec_and_ignored_names(tmp_path: Path) -> None:
    src = str(tmp_path / "drop")
    rule = WatchPortalRule(
        id="w-1",
        name="Auto Drop",
        source_dir=src,
        device_id=DEV_A,
        patterns=("*.png",),
        recursive=True,
    )
    assert watch_portal_from_dict(watch_portal_to_dict(rule)) == rule

    assert is_ignored_watch_name(".hidden")
    assert is_ignored_watch_name("~$doc.docx")
    assert is_ignored_watch_name("video.mp4.crdownload")
    assert is_ignored_watch_name("file.part")
    assert not is_ignored_watch_name("photo.PNG")

    with pytest.raises(ValueError):
        WatchPortalRule(id="w1", name="EmptyDev", source_dir=src, device_id="not-valid")


def test_watch_tracker_seed_debounce_change_and_no_delete_resurrection(tmp_path: Path) -> None:
    src = str(tmp_path / "drop")
    rule = WatchPortalRule(id="w1", name="Drop", source_dir=src, device_id=DEV_A, patterns=("*.bin", "*.txt"))
    tracker = WatchTracker([rule])

    # Lần quét đầu tiên seed tệp cũ mà không đẩy hàng loạt
    ready0 = tracker.scan("w1", {"old.bin": (100, 10), "ignore.tmp": (50, 10)}, now=1.0, stability=2.0)
    assert ready0 == []

    # Tệp mới xuất hiện ở t=2.0 -> chưa đủ stability
    snap = {"old.bin": (100, 10), "new.bin": (200, 20), "skip.jpg": (200, 20), "a.part": (200, 20)}
    assert tracker.scan("w1", snap, now=2.0, stability=2.0) == []
    assert tracker.scan("w1", snap, now=3.5, stability=2.0) == []

    # Kích thước đổi lúc t=3.5 -> đặt lại bộ đếm ổn định
    snap_growing = dict(snap, **{"new.bin": (250, 21)})
    assert tracker.scan("w1", snap_growing, now=3.5, stability=2.0) == []
    assert tracker.scan("w1", snap_growing, now=5.0, stability=2.0) == []
    assert tracker.scan("w1", snap_growing, now=5.6, stability=2.0) == ["new.bin"]

    # Đã đẩy rồi thì không đẩy lại khi chưa đổi
    assert tracker.scan("w1", snap_growing, now=10.0, stability=2.0) == []

    # Xóa khỏi thư mục nguồn chỉ dọn bảng theo dõi
    assert tracker.scan("w1", {"old.bin": (100, 10)}, now=11.0, stability=2.0) == []
