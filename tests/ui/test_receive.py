"""Nhận file qua GUI: hộp hỏi nhận file (`ui.offer`, mvp mục 7.3; trước là hộp tạm spec-p4-3 D7).

A là GUI (`main()` trong tiến trình, node 127.0.0.1 cổng 0, `auto_accept=False`); B là `Node` thật chạy
loop riêng trong thread (fixture `peer_node`), hai bên đã tin nhau. B gửi → A mở một hộp (`QDialog`) không
cha, `NonModal`, bằng `show` (không `exec()`), nút “Nhận”/“Từ chối”. Nhận → file đúng SHA-256;
Từ chối → B thấy `rejected_user`; B hủy → hộp tự đóng; tên máy và tên file qua `core.display`.
"""

from __future__ import annotations

import hashlib
import os

import pytest

pytest.importorskip("PySide6")

pytestmark = pytest.mark.integration

FILE_SIZE = 300_000


@pytest.fixture
def pair(config_dir, trust_each_other, tmp_path):
    """Hai thư mục cấu hình tin nhau; `name_b`: tên của B trong `trusted.json` của A."""

    def make(name_b: str = "May-B"):
        a, b = config_dir("A"), config_dir("B")
        trust_each_other(a, b, name_a="May-A", name_b=name_b)
        src = tmp_path / "nguon"
        src.mkdir(exist_ok=True)
        return a, b, src

    return make


def make_file(directory, name: str, size: int = FILE_SIZE):
    path = directory / name
    path.write_bytes(os.urandom(size))
    return path


def offer_boxes(ui_helpers) -> list:
    """Các hộp hỏi nhận file đang hiện: cửa sổ cấp cao nhất là `QDialog` có nút “Nhận”."""
    from PySide6.QtWidgets import QApplication, QDialog

    return [
        w
        for w in QApplication.topLevelWidgets()
        if isinstance(w, QDialog) and w.isVisible() and ui_helpers.buttons_with_text(w, ui_helpers.M6_ACCEPT)
    ]


def connect_peer(run, peer, gui_wait, a_id: str):
    """Kịch bản con: lấy cổng của A qua `NodeHost.call`, cho B nối kết nối điều khiển tới A."""
    got: list = []
    run.host.call(lambda node: node.port, got.append)
    yield gui_wait(lambda: got, 5, "cổng của A")
    port = got[0]
    assert isinstance(port, int), port
    peer.soon(peer.node.connect_hint, a_id, ["127.0.0.1"], port)
    yield gui_wait(lambda: peer.connected_to(a_id), 10, "B nối tới A")


def send(peer, a_id: str, *paths):
    return peer.submit(peer.node.transfers.send(a_id, list(paths)))


def received_file(receive_dir, name: str):
    p = receive_dir / name
    return p if p.is_file() else None


def test_accept_saves_file_with_correct_sha256(gui, gui_wait, pair, peer_node, tmp_path, ui_helpers):
    from teleport.trust.identity import load_or_create_identity

    a, b, src = pair()
    a_id = load_or_create_identity(a).device_id
    peer = peer_node(b)
    f = make_file(src, "video.bin")
    want = hashlib.sha256(f.read_bytes()).hexdigest()
    receive_dir = tmp_path / "nhan-gui"
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
        yield from connect_peer(run, peer, gui_wait, a_id)
        fut = send(peer, a_id, f)
        yield gui_wait(lambda: offer_boxes(ui_helpers), 10, "hộp hỏi mở")
        [box] = offer_boxes(ui_helpers)
        seen["parent"] = box.parent()
        seen["modality"] = box.windowModality()
        seen["text"] = ui_helpers.all_text(box)
        seen["reject_buttons"] = len(ui_helpers.buttons_with_text(box, ui_helpers.M6_REJECT))
        ui_helpers.buttons_with_text(box, ui_helpers.M6_ACCEPT)[0].click()
        yield gui_wait(fut.done, 10, "send() trả khóa")
        key = fut.result()
        yield gui_wait(lambda: peer.finished(key.transfer_id), 30, "B thấy lượt kết thúc")
        seen["result"] = str(peer.finished(key.transfer_id).result)
        yield gui_wait(lambda: received_file(receive_dir, "video.bin"), 10, "file trong thư mục nhận")
        seen["boxes_after"] = offer_boxes(ui_helpers)
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script)
    from PySide6.QtCore import Qt

    assert run.exec_calls == [], f"hộp hỏi không được mở bằng exec(): {run.exec_calls}"
    assert seen["parent"] is None, "hộp hỏi không có cha (mục 10)"
    assert seen["modality"] == Qt.WindowModality.NonModal
    assert seen["reject_buttons"] == 1
    assert seen["result"] == "done"
    assert hashlib.sha256((receive_dir / "video.bin").read_bytes()).hexdigest() == want
    assert seen["boxes_after"] == []


def test_offer_text_uses_m6_with_display_name_and_size(gui, gui_wait, pair, peer_node, ui_helpers):
    from teleport.core.display import display_filename, display_name, format_size
    from teleport.trust.identity import load_or_create_identity

    raw_name = "PC　Nha⠀B"  # khoảng trắng ý tự và chữ nổi trống: hợp lệ nhưng đánh lừa mắt
    a, b, src = pair(name_b=raw_name)
    a_id = load_or_create_identity(a).device_id
    peer = peer_node(b)
    raw_file = "bao⠀cao.txt"
    f = make_file(src, raw_file, 1234)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
        yield from connect_peer(run, peer, gui_wait, a_id)
        fut = send(peer, a_id, f)
        yield gui_wait(lambda: offer_boxes(ui_helpers), 10, "hộp hỏi mở")
        [box] = offer_boxes(ui_helpers)
        seen["raw"] = "\n".join(ui_helpers.widget_texts(box))
        seen["text"] = ui_helpers.all_text(box)
        ui_helpers.buttons_with_text(box, ui_helpers.M6_REJECT)[0].click()
        yield gui_wait(fut.done, 10, "send() trả khóa")
        yield gui_wait(lambda: peer.finished(fut.result().transfer_id), 15, "B thấy lượt kết thúc")
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script)
    shown_name = display_name(raw_name)
    # M6 chốt sau soat-code vòng 1: tên máy luôn trong ngoặc “…” (mvp 7.2, mvp §8).
    m6 = f"“{shown_name}” muốn gửi 1 file ({format_size(1234)}) tới máy này."
    assert m6 in seen["text"], seen["text"]
    assert display_filename(raw_file) in seen["text"]
    assert "　" not in seen["raw"] and "⠀" not in seen["raw"], "ký tự trống phải qua display_*"


def test_reject_makes_sender_see_rejected_user(gui, gui_wait, pair, peer_node, tmp_path, ui_helpers):
    from teleport.trust.identity import load_or_create_identity

    a, b, src = pair()
    a_id = load_or_create_identity(a).device_id
    peer = peer_node(b)
    f = make_file(src, "a.bin")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
        yield from connect_peer(run, peer, gui_wait, a_id)
        fut = send(peer, a_id, f)
        yield gui_wait(lambda: offer_boxes(ui_helpers), 10, "hộp hỏi mở")
        [box] = offer_boxes(ui_helpers)
        ui_helpers.buttons_with_text(box, ui_helpers.M6_REJECT)[0].click()
        yield gui_wait(fut.done, 10, "send() trả khóa")
        key = fut.result()
        yield gui_wait(lambda: peer.finished(key.transfer_id), 15, "B thấy lượt kết thúc")
        fin = peer.finished(key.transfer_id)
        seen["result"], seen["by_peer"] = str(fin.result), fin.by_peer
        yield gui_wait(lambda: not offer_boxes(ui_helpers), 3, "hộp hỏi đóng")
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script)
    assert seen["result"] == "rejected_user"
    assert seen["by_peer"] is True
    assert not (tmp_path / "nhan-gui" / "a.bin").exists()


def test_sender_cancel_closes_box(gui, gui_wait, pair, peer_node, ui_helpers):
    from teleport.trust.identity import load_or_create_identity

    a, b, src = pair()
    a_id = load_or_create_identity(a).device_id
    peer = peer_node(b)
    f = make_file(src, "a.bin")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
        yield from connect_peer(run, peer, gui_wait, a_id)
        fut = send(peer, a_id, f)
        yield gui_wait(lambda: offer_boxes(ui_helpers), 10, "hộp hỏi mở")
        [box] = offer_boxes(ui_helpers)
        yield gui_wait(fut.done, 10, "send() trả khóa")
        key = fut.result()
        peer.soon(peer.node.transfers.cancel, key)
        yield gui_wait(
            lambda: not ui_helpers.is_alive(box) or not box.isVisible(), 10, "hộp hỏi tự đóng khi B hủy"
        )
        seen["result"] = (
            str(peer.finished(key.transfer_id).result) if peer.finished(key.transfer_id) else None
        )
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script)
    assert seen["result"] == "cancelled_user"
    assert run.code == 0


def test_two_offers_get_two_boxes_and_tray_message(gui, gui_wait, pair, peer_node, ui_helpers, monkeypatch):
    """Mục 10: mỗi `key` một hộp; có khay thì kèm `showMessage` “có file đang chờ”.

    Từ PR-6 (B5) bóng thoại offer chỉ hiện khi app không được dùng: vá seam `GuiApp._app_active` trả
    False (app đang bị che). Ca ngược (active → không bóng thoại): tests/ui/test_quit_tray_address.py.
    """
    from teleport.trust.identity import load_or_create_identity
    from teleport.ui.app import GuiApp

    monkeypatch.setattr(GuiApp, "_app_active", lambda self: False)

    a, b, src = pair()
    a_id = load_or_create_identity(a).device_id
    peer = peer_node(b)
    f1, f2 = make_file(src, "a.bin"), make_file(src, "b.bin")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
        yield from connect_peer(run, peer, gui_wait, a_id)
        futs = [send(peer, a_id, f1), send(peer, a_id, f2)]
        yield gui_wait(lambda: all(fu.done() for fu in futs), 10, "send() trả khóa")
        keys = [fu.result() for fu in futs]
        boxes: list = []
        # Bên gửi có thể xếp hàng lượt thứ hai: hai hộp cùng lúc hay lần lượt đều được, miễn mỗi lượt
        # một hộp riêng.
        while not all(peer.finished(k.transfer_id) for k in keys):
            yield gui_wait(
                lambda: offer_boxes(ui_helpers) or all(peer.finished(k.transfer_id) for k in keys),
                15,
                "hộp hỏi kế tiếp",
            )
            seen.setdefault("messages", list(run.messages))
            for box in offer_boxes(ui_helpers):
                if all(box is not b for b in boxes):
                    boxes.append(box)
                seen["max_open"] = max(seen.get("max_open", 0), len(offer_boxes(ui_helpers)))
                ui_helpers.buttons_with_text(box, ui_helpers.M6_REJECT)[0].click()
            yield
        seen["boxes"] = len(boxes)
        seen["results"] = [str(peer.finished(k.transfer_id).result) for k in keys]
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script)
    assert seen["messages"], "có khay thì báo bằng showMessage khi có file đang chờ"
    assert seen["boxes"] == 2, "mỗi lượt một hộp hỏi riêng"
    assert seen["results"] == ["rejected_user", "rejected_user"]


def test_many_files_listed_at_most_ten(gui, gui_wait, pair, peer_node, ui_helpers):
    """mvp mục 7.3: liệt kê tối đa 10 tên file rồi “… và k file khác”."""
    from teleport.trust.identity import load_or_create_identity

    a, b, src = pair()
    a_id = load_or_create_identity(a).device_id
    peer = peer_node(b)
    files = [make_file(src, f"tep-{i:02}.bin", 100) for i in range(12)]
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
        yield from connect_peer(run, peer, gui_wait, a_id)
        send(peer, a_id, *files)
        yield gui_wait(lambda: offer_boxes(ui_helpers), 10, "hộp hỏi mở")
        [box] = offer_boxes(ui_helpers)
        seen["text"] = ui_helpers.all_text(box)
        ui_helpers.buttons_with_text(box, ui_helpers.M6_REJECT)[0].click()
        yield gui_wait(lambda: not offer_boxes(ui_helpers), 5, "hộp đóng")
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script)
    text = seen["text"]
    listed = [f.name for f in files if f.name in text]
    assert len(listed) == 10, f"liệt kê {len(listed)} tên: {text!r}"
    assert "muốn gửi 12 file" in text
    assert "và 2 file khác" in text
