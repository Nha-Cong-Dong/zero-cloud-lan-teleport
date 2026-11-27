"""Kiểm thử `teleport.ui.thumbnails.ThumbnailLoader` (giải mã nền, LRU cache, giới hạn kích thước)."""

from __future__ import annotations

from PySide6.QtGui import QColor, QImage, QPixmap

from teleport.ui import thumbnails
from teleport.ui.thumbnails import ThumbnailLoader


def _make_png(path, width: int = 240, height: int = 160) -> None:
    img = QImage(width, height, QImage.Format.Format_RGBA8888)
    img.fill(QColor(30, 144, 255))
    assert img.save(str(path), "PNG")


def test_thumbnail_loader_decodes_async_caches_and_scales(qapp, qtbot, tmp_path):
    p = tmp_path / "photo.png"
    _make_png(p, 320, 160)
    loader = ThumbnailLoader(limit=4)
    try:
        got: list[QPixmap | None] = []
        loader.request(p, got.append, size=72)
        qtbot.waitUntil(lambda: len(got) == 1, timeout=3000)
        pix = got[0]
        assert isinstance(pix, QPixmap) and not pix.isNull()
        assert max(pix.width(), pix.height()) <= 72

        # Lần hai trả ngay từ đệm (đồng bộ).
        second: list[QPixmap | None] = []
        loader.request(p, second.append, size=72)
        assert len(second) == 1 and second[0] is not None
        assert loader.cached(p, 72) is not None
    finally:
        loader.shutdown()


def test_thumbnail_loader_lru_evicts_oldest_and_skips_non_images(qapp, qtbot, tmp_path):
    p1, p2, p3 = tmp_path / "a.png", tmp_path / "b.png", tmp_path / "c.png"
    for p in (p1, p2, p3):
        _make_png(p, 100, 100)
    txt = tmp_path / "note.txt"
    txt.write_text("hi", encoding="utf-8")

    loader = ThumbnailLoader(limit=2)
    try:
        called = 0

        def on_txt(_pix):
            nonlocal called
            called += 1

        loader.request(txt, on_txt)
        assert called == 0

        res: list = []
        for p in (p1, p2):
            loader.request(p, res.append, size=48)
        qtbot.waitUntil(lambda: len(res) == 2, timeout=3000)
        # Truy cập lại p1 để p2 trở thành mục cũ nhất -> nạp p3 sẽ đẩy p2.
        loader.request(p1, lambda _pix: None, size=48)
        loader.request(p3, res.append, size=48)
        qtbot.waitUntil(lambda: len(res) == 3, timeout=3000)
        assert loader.cached(p1, 48) is not None
        assert loader.cached(p2, 48) is None
        assert loader.cached(p3, 48) is not None
    finally:
        loader.shutdown()


def test_thumbnail_loader_respects_max_file_bytes(qapp, qtbot, tmp_path, monkeypatch):
    p = tmp_path / "big.png"
    _make_png(p, 120, 120)
    monkeypatch.setattr(thumbnails, "THUMB_MAX_FILE_BYTES", 10)
    loader = ThumbnailLoader()
    try:
        got: list = []
        loader.request(p, got.append)
        qtbot.waitUntil(lambda: len(got) == 1, timeout=3000)
        assert got == [None]
        assert loader.cached(p) is None
    finally:
        loader.shutdown()
