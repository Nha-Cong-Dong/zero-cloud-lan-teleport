"""Cảnh giao diện dựng sẵn cho các test soát toàn màn (spec E1 mục 2.2, 2.3, 2.5).

Import bằng `from ui_scenes import …` nhờ `pythonpath = ["tests"]` (file test không import lẫn nhau,
`docs/architecture.md` mục 6). Dùng bởi `tests/ui/test_translations.py` (English: không chữ Việt, không
tràn chữ) và `tests/ui/test_a11y.py` (trợ năng N12).

- **`SCENES`**: tên cảnh → `build(qtbot) -> QWidget` trả cửa sổ chính (`MainWindow`) đã dựng ở một
  trạng thái, **chưa hiện** (bên gọi tự đặt cỡ và hiện). Tên cảnh cũ (dời nguyên từ
  `test_translations.py`) giữ nguyên: tham số của test cũ không đổi.
- **`DIALOGS`**: tên hộp → `build(qtbot) -> QWidget` trả một hộp thoại cấp cao nhất (`QDialog` /
  `QMessageBox`). Hộp do bộ điều khiển mở bằng `show()`/`open()` thì đã hiện; bên gọi vẫn gọi `show()`
  và `adjustSize()`. Đối tượng phải sống cùng hộp (bộ điều khiển, sổ lời mời) gắn vào thuộc tính
  `_scene_keep` của hộp.

Module `teleport.*` chỉ import **bên trong** hàm dựng (như `tests/ui/conftest.py`): module `ui` đang viết
dở chỉ làm đỏ cảnh dùng tới nó. Dữ liệu của cảnh (tên máy, đường dẫn, tên file) chỉ dùng chữ ASCII, để
test “không còn chữ Việt” không cần ngoại lệ cho dữ liệu.
"""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Callable
from pathlib import Path

RECEIVE_DIR = Path("C:/Users/Minh/Downloads/Teleport")

Builder = Callable[..., object]


# =================================================================================================
# Cửa sổ chính
# =================================================================================================


def _device(name: str, *, paired: bool, online: bool = True, connected: bool = False, os_name="windows"):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=hashlib.sha256(name.encode()).hexdigest()[:32],
        name=name,
        os=os_name if online else None,
        paired=paired,
        online=online,
        connected=connected,
        addresses=("192.168.1.20",) if online else (),
        port=47474 if online else None,
    )


def _window(qtbot):
    from teleport.ui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    return window


def _running(qtbot, views=()):
    window = _window(qtbot)
    window.devices.reset(list(views))
    window.show_running("Laptop-Minh", RECEIVE_DIR)
    window.set_addresses(["192.168.1.15"], None)
    return window


def _some_devices():
    return [
        _device("PC-Nha", paired=True, connected=True),
        _device("MacBook-Lan", paired=True, os_name="macos"),
        _device("Ubuntu-Lab", paired=True, online=False),
        _device("Laptop-Khach", paired=False),
    ]


def scene_welcome(qtbot):
    window = _window(qtbot)
    window.show_welcome("Laptop-Minh")
    return window


def scene_starting(qtbot):
    window = _window(qtbot)
    window.show_starting()
    return window


def scene_running_empty(qtbot):
    return _running(qtbot)


def scene_running_unavailable(qtbot):
    window = _window(qtbot)
    window.show_running("Laptop-Minh", None, Path("E:/Teleport"))
    window.set_addresses(["192.168.1.15"], None)
    return window


def _chat(window, views, device):
    """`ChatView` đặt vào khung phải như `TransfersController` (spec B3), đang mở luồng của `device`."""
    from teleport.ui.chat import ChatView

    view = ChatView(window.devices, window.transfers_area)
    layout = window.transfers_area.layout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(view)
    view.content_changed.connect(lambda: window.set_transfer_summary(view.has_content(), view.active_count()))
    window.conversations.current_changed.connect(view.set_device)
    view.set_device_names({v.device_id: v.name for v in views})
    view.set_receive_dir(RECEIVE_DIR)
    view.history_needed.connect(lambda device_id: view.load_history(device_id, []))
    assert window.open_conversation(device.device_id)
    view.set_device(device.device_id)
    return view


def _transfer(device_id: str, direction: str, i: int, status: str, name: str, size: int, done: int = 0):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import Direction, TransferInfo, TransferKey, TransferStatus

    return TransferInfo(
        key=TransferKey(device_id, Direction(direction), f"{i:032x}"),
        status=TransferStatus(status),
        files=(FileEntry(name, size, 1_700_000_000_000),),
        names=(name,),
        file_index=0,
        done_bytes=done,
        total_bytes=size,
        unsynced_bytes=0,
    )


def _finished(info, result: str):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=info, result=TransferResult(result), by_peer=False, reason=None, file_index=0, reject=None
    )


def _history(device, i: int, direction: str, name: str, result: str, days_ago: int):
    import datetime

    from teleport.core.history import HistoryEntry
    from teleport.core.transfer import Direction

    at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)
    return HistoryEntry(
        direction=Direction(direction),
        device_id=device.device_id,
        device_name=device.name,
        transfer_id=f"{i:032x}",
        file_count=1,
        total_bytes=420 * 2**20,
        done_bytes=420 * 2**20 if result == "done" else 0,
        names=(name,),
        result=result,
        finished_at=at,
        saved_path=str(RECEIVE_DIR / name) if direction == "receive" and result == "done" else None,
    )


def scene_conversation(qtbot):
    """Luồng chat (spec B3) của máy đang chọn có đủ loại bong bóng: lịch sử (xong, lỗi, đã từ chối, hôm
    qua và một năm trước), nhận xong (“Hiện trong thư mục”), gửi lỗi có “Gửi lại”, đang gửi (tốc độ, còn …),
    đang chờ, bong bóng tạm “Đang chuẩn bị…”, dòng mốc, nút “↓ Mới” và thông báo có nút hành động."""
    from teleport.app.transfers import FileDone, TransferProgress, TransferStatusChanged
    from teleport.ui import texts

    views = _some_devices()
    window = _running(qtbot, views)
    nha = views[0]
    view = _chat(window, views, nha)
    view._history_timer.stop()
    view.history_needed.disconnect()
    view.history_needed.connect(
        lambda device_id: view.load_history(
            device_id,
            [
                _history(nha, 1, "receive", "slide.pptx", "done", 400),
                _history(nha, 2, "send", "old-movie.mkv", "rejected_user", 1),
                _history(nha, 3, "receive", "music.mp3", "disk_failed", 1),
            ],
        )
    )
    view._request_history()
    received = _transfer(
        nha.device_id, "receive", 10, "verifying", "photos.zip", 1_800 * 2**20, 1_800 * 2**20
    )
    view.handle(TransferStatusChanged(received))
    view.handle(FileDone(received.key, 0, "photos.zip", RECEIVE_DIR / "photos.zip"))
    view.handle(_finished(received, "done"))
    report = _transfer(nha.device_id, "send", 11, "transferring", "report.pdf", 4 * 2**20, 2**20)
    view.handle(TransferStatusChanged(report))
    view.handle(_finished(report, "source_changed"))
    view.set_resendable(report.key, True)
    size = 18 * 2**30
    sending = _transfer(nha.device_id, "send", 12, "transferring", "movie-4k.mkv", size, size // 2)
    view.handle(TransferStatusChanged(sending))
    view.handle(TransferProgress(sending, 94 * 2**20, 60.0, shown_bytes=sending.done_bytes, idle_seconds=0.0))
    waiting = _transfer(nha.device_id, "receive", 13, "waiting", "contract.docx", 2**20)
    view.handle(TransferStatusChanged(waiting))
    view.add_pending(nha.device_id, [Path("C:/Users/Minh/Videos/slides.pptx")])
    view._set_new_count(2)  # như khi đã cuộn lên rồi có lượt mới
    view.show_notice(texts.menu_open, texts.retry, lambda: None)  # câu dựng bằng hàm: dịch lại được
    thread = view.thread_for(nha.device_id)
    assert thread is not None and len(thread.history_bubbles()) == 3 and len(view.rows()) == 4
    return window


def scene_chat_offline(qtbot):
    """Máy đã ghép đôi đang ngoại tuyến: lượt gửi chờ kết nối, câu ngoại tuyến ở thanh dưới."""
    from teleport.app.transfers import TransferStatusChanged

    views = _some_devices()
    window = _running(qtbot, views)
    lab = views[2]
    view = _chat(window, views, lab)
    view.set_connected(lab.device_id, False)
    view.handle(TransferStatusChanged(_transfer(lab.device_id, "send", 20, "waiting", "demo.mp4", 3 * 2**30)))
    return window


def scene_chat_empty(qtbot):
    """Máy chưa có lượt nào: màn rỗng “Kéo file vào đây để gửi cho X”."""
    views = _some_devices()
    window = _running(qtbot, views)
    view = _chat(window, views, views[1])
    view._history_timer.stop()
    view._request_history()
    return window


def _error_scene(code: str):
    def build(qtbot):
        window = _window(qtbot)
        window.show_error(code, "C:/Teleport\n[WinError 5] Access is denied" if code == "config_dir" else "")
        return window

    return build


def _discover_scene(views_factory):
    def build(qtbot):
        from teleport.ui.main_window import Page

        window = _running(qtbot, views_factory())
        assert window.open_page(Page.DISCOVER)
        return window

    return build


def scene_address_entry(qtbot):
    window = _running(qtbot)
    assert window.open_address_entry()
    return window


def _settings_scene(section_name: str, views_factory=_some_devices):
    def build(qtbot):
        from teleport.app import autostart
        from teleport.app.settings import DeviceSettings
        from teleport.ui.main_window import Page
        from teleport.ui.settings import Section, SettingsPage, SettingsSnapshot

        views = views_factory()
        window = _running(qtbot, views)
        page = SettingsPage(window.devices, window.pages)
        window.add_page(Page.SETTINGS, page)
        page.set_values(
            SettingsSnapshot(
                name="Laptop-Minh",
                visible=section_name != "privacy",  # mục Riêng tư: máy đang ẩn
                rate_limit=20 * 1024 * 1024,
                temp_retention_days=None,
                language=None,
                autostart=True,
                devices={v.device_id: DeviceSettings(ask_before_receive=True) for v in views if v.paired},
            )
        )
        page.set_language(None, ("en", "vi"))
        page.set_autostart(autostart.AutostartState.ON)
        page.set_receive_dir(RECEIVE_DIR)
        page.set_change_available(True)
        page.set_loaded(True)
        assert window.open_page(Page.SETTINGS)
        page.show_section(Section(section_name))
        return window

    return build


SETTINGS_SECTIONS = ("general", "appearance", "privacy", "devices", "transfer", "advanced")
START_ERRORS = ("locked", "config_dir", "identity", "trust_store", "receive_dir", "port", "other")


# ---- Cảnh mới của spec E1 mục 2.2 ---------------------------------------------------------------


def scene_welcome_name_error(qtbot):
    """Lần chạy đầu, gợi ý tên rỗng, bấm “Bắt đầu”: lỗi tên dưới ô; hàng khởi động cùng Windows hiện, bật."""
    window = _window(qtbot)
    window.show_welcome("", RECEIVE_DIR, autostart=True)
    assert window.first_run.validate() is None
    assert window.first_run.name_error
    return window


def scene_welcome_dir_error(qtbot):
    """Lần chạy đầu sau khi node không mở được thư mục đã chọn: lỗi thư mục dưới hàng “Đổi…”.

    Câu lỗi truyền dạng hàm (như `ui.app` truyền `TextSource`), nên đổi ngôn ngữ lúc chạy thì dịch lại."""
    from teleport.ui.first_run import FirstRunChoice
    from teleport.ui.receive_dir import ChangeError, change_error_text

    chosen = Path("E:/USB-Drive/Teleport-files-from-other-machines")
    window = _window(qtbot)
    window.show_welcome(
        "Laptop-Minh",
        RECEIVE_DIR,
        autostart=True,
        choice=FirstRunChoice(
            name_text="Laptop-Minh", name=None, receive_dir=chosen, receive_dir_chosen=True, autostart=True
        ),
        dir_error=functools.partial(change_error_text, ChangeError.UNAVAILABLE, chosen),
    )
    assert window.first_run.dir_error
    return window


def scene_welcome_no_autostart(qtbot):
    """Lần chạy đầu khi Windows chặn ghi khởi động: công tắc tắt, câu trạng thái cảnh báo hiện.

    (Cảnh `welcome` cũ đã phủ trường hợp hàng tự khởi động bị ẩn.)"""
    from teleport.app.autostart import AutostartState
    from teleport.ui.settings import autostart_state_hint

    window = _window(qtbot)
    window.show_welcome(
        "Laptop-Minh",
        RECEIVE_DIR,
        autostart=False,
        autostart_hint=functools.partial(autostart_state_hint, AutostartState.BLOCKED),
    )
    return window


def _wait_pairing_scene(hidden: bool):
    def build(qtbot):
        from teleport.ui.main_window import Page
        from teleport.ui.wait_pairing import WaitPairingPage

        window = _running(qtbot, _some_devices())
        page = WaitPairingPage()
        window.add_page(Page.WAIT_PAIRING, page)
        page.back_requested.connect(window.go_back)
        assert window.open_page(Page.WAIT_PAIRING)
        page.set_identity("Laptop-Minh", "a71c04d2")
        page.set_hidden(hidden)
        page.set_waiting(True)
        page.set_addresses(["192.168.1.15", "10.0.0.5"], 47475)
        return window

    return build


def scene_discover_notice(qtbot):
    """“Ghép đôi lại” khi radar không thấy máy (mDNS bị chặn): trang Tìm máy mới có câu mời nhập IP.

    Như ở app, câu hiện khi cửa sổ **đã hiện**: lần hiện đầu của trang (`DiscoverPage.showEvent`) xóa thanh
    thông báo, nên cảnh hiện cửa sổ (không lên màn hình) trước khi gọi `open_address_entry`."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from teleport.ui.pairing import repair_offline_text

    window = _running(qtbot, _some_devices())
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.show()
    QApplication.processEvents()
    assert window.open_address_entry(functools.partial(repair_offline_text, "PC-Nha"))
    assert window.discover.info_bar.isVisibleTo(window)
    return window


def _offer(device_id: str, names_sizes, transfer_id: str = "d" * 32):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import Direction, TransferKey

    files = tuple(FileEntry(name, size, 1_700_000_000_000) for name, size in names_sizes)
    return OfferReceived(
        key=TransferKey(device_id, Direction.RECEIVE, transfer_id),
        device_name="PC-Nha",
        files=files,
        names=tuple(name for name, _ in names_sizes),
        total_bytes=sum(size for _, size in names_sizes),
        auto_accepted=False,
    )


_OFFER_FILES = (
    ("wedding-video.mp4", 8 * 2**30),
    ("cover.png", int(2.1 * 2**20)),
    ("plan.docx", 380 * 1024),
)
_LONG_NAMES = tuple(
    (f"Quarterly-report-for-the-board-meeting-final-version-{i:02d}-reviewed-and-approved.xlsx", 3 * 2**20)
    for i in range(14)
)


def scene_chat_offer(qtbot):
    """Luồng của PC-Nha có lời mời nhận file đang chờ trả lời (spec B4): panel Nhận / Từ chối."""
    from teleport.ui.offer import OfferBook

    views = _some_devices()
    window = _running(qtbot, views)
    nha = views[0]
    view = _chat(window, views, nha)
    book = OfferBook(window)
    view.set_offer_book(book)
    item = _offer(nha.device_id, _OFFER_FILES)
    view.handle(item)
    assert book.add(item)
    return window


def scene_chat_clipboard_note(qtbot):
    """Luồng của PC-Nha có dòng báo clipboard: ảnh nhận được mà chưa chép được (nút thử lại)."""
    from teleport.core.messages import ClipKind

    views = _some_devices()
    window = _running(qtbot, views)
    nha = views[0]
    view = _chat(window, views, nha)
    view.show_clipboard(nha.device_id, ok=False, kind=ClipKind.PNG, size=3 * 2**20, image_size=(1920, 1080))
    return window


SCENES: dict[str, Builder] = {
    "welcome": scene_welcome,
    "starting": scene_starting,
    "running_empty": scene_running_empty,
    "running_unavailable": scene_running_unavailable,
    "conversation": scene_conversation,
    "chat_offline": scene_chat_offline,
    "chat_empty": scene_chat_empty,
    "discover": _discover_scene(_some_devices),
    "discover_empty": _discover_scene(list),
    "discover_list": _discover_scene(lambda: [_device(f"PC-{i}", paired=False) for i in range(9)]),
    "address_entry": scene_address_entry,
    **{f"error_{code}": _error_scene(code) for code in START_ERRORS},
    **{f"settings_{name}": _settings_scene(name) for name in SETTINGS_SECTIONS},
    # Spec E1 mục 2.2
    "welcome_name_error": scene_welcome_name_error,
    "welcome_dir_error": scene_welcome_dir_error,
    "welcome_no_autostart": scene_welcome_no_autostart,
    "wait_pairing": _wait_pairing_scene(hidden=False),
    "wait_pairing_hidden": _wait_pairing_scene(hidden=True),
    "settings_devices_empty": _settings_scene("devices", lambda: [_device("Laptop-Khach", paired=False)]),
    "discover_notice": scene_discover_notice,
    "chat_offer": scene_chat_offer,
    "chat_clipboard_note": scene_chat_clipboard_note,
}


# =================================================================================================
# Hộp thoại
# =================================================================================================


def _keep(box, *objects):
    """Giữ sống các đối tượng mà hộp cần (bộ điều khiển, sổ lời mời) tới khi hộp bị dọn."""
    box._scene_keep = objects
    return box


def _prompt(role: str, *, recent_codes: int = 0, known_name: str | None = None):
    from teleport.app.pairing import PairingPrompt
    from teleport.core.pairing import Role

    return PairingPrompt(
        session_id=1,
        role=Role(role),
        address="192.168.1.20",
        peer_name="PC-Nha",
        peer_device_id=hashlib.sha256(b"PC-Nha").hexdigest()[:32],
        code="382104",
        expires_at=100.0,
        recent_codes=recent_codes,
        known_name=known_name,
    )


def _pairing_dialog(role: str, *, warn: bool = False):
    def build(qtbot):
        from teleport.core import pairing as core_pairing
        from teleport.ui.pairing import PairingDialog

        recent = core_pairing.WARN_RECENT_CODES if warn else (1 if role == "responder" else 0)
        dialog = PairingDialog(_prompt(role, recent_codes=recent), clock=lambda: 70.0)
        qtbot.addWidget(dialog)
        return dialog

    return build


def dialog_pairing_progress(qtbot):
    from teleport.ui.pairing import PairingProgressDialog

    dialog = PairingProgressDialog("PC-Nha")
    qtbot.addWidget(dialog)
    return dialog


def _pairing_host():
    """Host giả tối thiểu cho `PairingController`: hai signal như `NodeHost`, `call`/`submit` bỏ qua."""
    from PySide6.QtCore import QObject, Signal

    class Host(QObject):
        pairing_prompt = Signal(object)
        pairing_finished = Signal(object)

        def call(self, fn, on_done=None) -> None:
            pass

        def submit(self, fn, on_done=None):
            return None

    return Host()


def dialog_pairing_result(qtbot):
    """Hộp kết quả lỗi của lần ghép do máy này chủ động (máy kia từ chối): [Thử lại] [Đóng]."""
    from teleport.app.pairing import PairingFinished, PairingOutcome
    from teleport.core.pairing import Role
    from teleport.ui.pairing import PairingController

    controller = PairingController()
    host = _pairing_host()
    controller.set_host(host)
    host.pairing_finished.emit(
        PairingFinished(
            session_id=1,
            role=Role.INITIATOR,
            outcome=PairingOutcome.PEER_REJECTED,
            address="192.168.1.20",
            port=47474,
            peer_name="PC-Nha",
            peer_device_id=hashlib.sha256(b"PC-Nha").hexdigest()[:32],
            peer_reason=None,
            device=None,
            known_name=None,
            code_shown=True,
        )
    )
    box = controller.result_box
    assert box is not None, "PEER_REJECTED ở vai mời phải mở hộp kết quả"
    qtbot.addWidget(box)
    return _keep(box, controller, host)


def _offer_box(names_sizes):
    def build(qtbot):
        from teleport.ui.offer import OfferBook, OfferPrompts

        book = OfferBook()
        prompts = OfferPrompts(book, RECEIVE_DIR)
        item = _offer(hashlib.sha256(b"PC-Nha").hexdigest()[:32], names_sizes)
        assert book.add(item)
        box = prompts.preview(item.key)
        assert box is not None
        qtbot.addWidget(box)
        return _keep(box, book, prompts)

    return build


def dialog_clear_history(qtbot):
    """Hộp xác nhận “Xóa toàn bộ lịch sử” của Cài đặt (`SettingsController.request_clear_history`)."""
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import SettingsController, SettingsPage

    model = DeviceListModel()
    page = SettingsPage(model)
    qtbot.addWidget(page)
    controller = SettingsController(page, Path("C:/Users/Minh/AppData/Roaming/Teleport"))
    controller.request_clear_history()
    box = controller.confirm_box
    assert box is not None
    qtbot.addWidget(box)
    return _keep(box, controller, model)


def dialog_unpair_confirm(qtbot):
    """Hộp xác nhận hủy ghép đôi (`PairingController.request_unpair`), có ô xóa cả lịch sử."""
    from teleport.ui.pairing import PairingController

    controller = PairingController()
    controller.request_unpair(hashlib.sha256(b"PC-Nha").hexdigest()[:32], "PC-Nha")
    box = controller.unpair_box
    assert box is not None
    qtbot.addWidget(box)
    return _keep(box, controller)


DIALOGS: dict[str, Builder] = {
    "pairing_initiator": _pairing_dialog("initiator"),
    "pairing_responder": _pairing_dialog("responder"),
    "pairing_responder_many_codes": _pairing_dialog("responder", warn=True),
    "pairing_progress": dialog_pairing_progress,
    "pairing_result": dialog_pairing_result,
    "offer_one_file": _offer_box(_OFFER_FILES[:1]),
    "offer_many_long_names": _offer_box(_LONG_NAMES),
    "clear_history_confirm": dialog_clear_history,
    "unpair_confirm": dialog_unpair_confirm,
}
