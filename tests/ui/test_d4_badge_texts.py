"""Huy hiệu “Đã tạm dừng nhận” của thẻ máy này và câu `not_receiving` phía gửi (spec D4 mục 4.3, 5.3,
mục 12 A4 phần huy hiệu, B5; 14.6, 14.8, 14.14, 14.16 thắng).

Viết từ spec, không từ code.

- `MachineCard.set_receiving_paused(bool)`: ưu tiên đang truyền > Không nhận file > **Đã tạm dừng nhận**
  (WARNING) > Đang ẩn > Sẵn sàng nhận (bảng của thẻ khác bảng của khay có chủ ý, 14.16). Mô tả trợ năng
  14.6 có tên mục menu `texts.menu_pause()`.
- Câu giao diện khi máy nhận trả `not_receiving` (14.14) ở VN và EN; câu CLI không đổi; câu giao diện
  trong `docs/mvp.md` mục 8 khớp code (tài liệu không lệch).
"""

from __future__ import annotations

import itertools
import re
import uuid
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

PEER_ID = "a71c04d2" + "e" * 24
_MVP = Path(__file__).resolve().parents[2] / "docs" / "mvp.md"

BUSY = "Đang truyền {n} lượt"
NOT_RECEIVING = "Không nhận file"
PAUSED = "Đã tạm dừng nhận"
HIDDEN = "Đang ẩn"
READY = "Sẵn sàng nhận"
PAUSED_DESCRIPTION_VI = (
    "File mới gửi tới sẽ bị từ chối; lượt đang nhận vẫn tiếp tục. "
    "Bỏ chọn “Tạm dừng nhận” trong menu khay để nhận lại."
)
PAUSED_DESCRIPTION_EN = (
    "New files sent to this device will be declined; transfers in progress continue. "
    "Uncheck “Pause receiving” in the tray menu to receive again."
)

GUI_NOT_RECEIVING_VI = (
    "{name} đang không nhận file mới (có thể đã tạm dừng nhận, hoặc chưa mở được thư mục nhận). "
    "Hãy nhờ bật lại việc nhận file trên máy đó rồi gửi lại."
)
GUI_NOT_RECEIVING_EN = (
    "{name} isn't accepting new files right now (receiving may be paused, or its receive folder can't be "
    "opened). Ask them to turn receiving back on, then send again."
)
CLI_NOT_RECEIVING = "{name} chưa bật nhận file. Trên máy đó hãy chạy `teleport receive` rồi gửi lại."


def descriptions(card) -> str:
    return " | ".join(
        (
            card.badge.accessibleDescription(),
            card.name_label.accessibleDescription(),
            card.accessibleDescription(),
        )
    )


@pytest.fixture
def card(qtbot):
    from teleport.ui.home import MachineCard

    c = MachineCard()
    qtbot.addWidget(c)
    c.set_name("May-A")
    return c


# =================================================================================================
# Huy hiệu thẻ máy này
# =================================================================================================


def expected(active: int, unavailable: bool, paused: bool, hidden: bool):
    from teleport.ui.widgets import BadgeTone

    if active > 0:
        return BUSY.format(n=active), BadgeTone.BUSY
    if unavailable:
        return NOT_RECEIVING, BadgeTone.WARNING
    if paused:
        return PAUSED, BadgeTone.WARNING
    if hidden:
        return HIDDEN, BadgeTone.MUTED
    return READY, BadgeTone.SUCCESS


def test_machine_badge_priority_with_paused_in_every_order(card):
    setters = {
        "active": card.set_activity,
        "unavailable": card.set_receiving_unavailable,
        "paused": card.set_receiving_paused,
        "hidden": card.set_hidden,
    }
    for active, unavailable, paused, hidden in itertools.product((0, 2), *([(False, True)] * 3)):
        values = {"active": active, "unavailable": unavailable, "paused": paused, "hidden": hidden}
        for order in itertools.permutations(setters):
            card.set_activity(0)
            card.set_receiving_unavailable(False)
            card.set_receiving_paused(False)
            card.set_hidden(False)
            for name in order:
                setters[name](values[name])
            got = (card.badge.text, card.badge.tone)
            assert got == expected(active, unavailable, paused, hidden), (values, order, got)


def test_paused_badge_has_accessible_description_with_menu_name(card):
    from teleport.ui import texts

    card.set_receiving_paused(True)
    assert card.badge.text == PAUSED
    assert PAUSED_DESCRIPTION_VI in descriptions(card), "trạng thái đọc được bằng chữ (N12)"
    assert f"“{texts.menu_pause()}”" in PAUSED_DESCRIPTION_VI
    card.set_receiving_paused(False)
    assert PAUSED_DESCRIPTION_VI not in descriptions(card)


def test_paused_description_dropped_when_higher_badge_wins(card):
    card.set_receiving_paused(True)
    card.set_activity(1)
    assert PAUSED_DESCRIPTION_VI not in card.badge.accessibleDescription()
    card.set_activity(0)
    card.set_receiving_unavailable(True)
    assert card.badge.text == NOT_RECEIVING
    assert PAUSED_DESCRIPTION_VI not in card.badge.accessibleDescription()
    card.set_receiving_unavailable(False)
    assert card.badge.text == PAUSED


def test_paused_badge_language_change(qtbot, qapp, tmp_path):
    """LanguageChange (đi qua `MainWindow` RUNNING như app thật): chữ huy hiệu và mô tả sang EN."""
    from teleport.ui import i18n
    from teleport.ui.main_window import MainWindow
    from teleport.ui.widgets import BadgeTone

    window = MainWindow()
    qtbot.addWidget(window)
    rd = tmp_path / "nhan"
    rd.mkdir()
    window.show_running("May-A", rd)
    window.resize(980, 660)
    window.show()
    window.set_receiving_paused(True)
    card = window.machine_card
    assert (card.badge.text, card.badge.tone) == (PAUSED, BadgeTone.WARNING)
    i18n.set_language("en")
    qtbot.waitUntil(lambda: card.badge.text != PAUSED, timeout=3000)
    assert card.badge.tone == BadgeTone.WARNING
    qtbot.waitUntil(lambda: PAUSED_DESCRIPTION_EN in descriptions(card), timeout=3000)
    i18n.set_language("vi")
    qtbot.waitUntil(lambda: card.badge.text == PAUSED, timeout=3000)
    assert PAUSED_DESCRIPTION_VI in descriptions(card)


def test_card_and_tray_priorities_differ_on_purpose(card):
    """14.16: đang truyền + tạm dừng → thẻ nói “Đang truyền”, khay báo PAUSED."""
    from teleport.ui.tray import TrayBadge, TrayState, tray_badge

    card.set_activity(2)
    card.set_receiving_paused(True)
    assert card.badge.text == BUSY.format(n=2)
    assert tray_badge(TrayState(running=True, active=2, paused=True)) is TrayBadge.PAUSED


# =================================================================================================
# B5: câu `not_receiving`
# =================================================================================================


def not_receiving_finished():
    from teleport.app.transfers import TransferFinished, TransferResult
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import Direction, TransferInfo, TransferKey, TransferStatus

    key = TransferKey(PEER_ID, Direction.SEND, uuid.uuid4().hex)
    info = TransferInfo(
        key=key,
        status=TransferStatus.WAITING,
        files=(FileEntry("video.mp4", 1000, 0),),
        names=("video.mp4",),
        file_index=0,
        done_bytes=0,
        total_bytes=1000,
    )
    return TransferFinished(
        info=info,
        result=TransferResult.REJECTED_NOT_RECEIVING,
        by_peer=True,
        reason=None,
        file_index=None,
        reject=None,
    )


def test_gui_not_receiving_sentence_vi(qapp):
    from teleport.ui import texts
    from teleport.ui.transfers_view import result_message

    peer = texts.peer_label("PC-Nha", PEER_ID)
    assert result_message(not_receiving_finished(), peer) == GUI_NOT_RECEIVING_VI.format(name=peer)


def test_gui_not_receiving_sentence_en(qapp):
    from teleport.ui import i18n, texts
    from teleport.ui.transfers_view import result_message

    i18n.set_language("en")
    peer = texts.peer_label("PC-Nha", PEER_ID)
    assert result_message(not_receiving_finished(), peer) == GUI_NOT_RECEIVING_EN.format(name=peer)


def test_cli_not_receiving_sentence_unchanged():
    from teleport import cli

    lines = cli.transfer_result_message(not_receiving_finished(), peer_name="PC-Nha", receive_dir=None)
    assert lines == [CLI_NOT_RECEIVING.format(name="PC-Nha")]


def test_mvp_doc_gui_sentence_matches_code(qapp):
    """`docs/mvp.md` mục 8, dòng “Máy nhận chưa bật nhận file”: câu sau “Giao diện:” khớp câu của code."""
    from teleport.ui.transfers_view import result_message

    text = _MVP.read_text(encoding="utf-8")
    start = text.index("## 8.")
    nxt = text.find("\n## ", start + 5)
    table = text[start : nxt if nxt != -1 else len(text)]
    rows = [line for line in table.splitlines() if line.startswith("| Máy nhận chưa bật nhận file")]
    assert len(rows) == 1, rows
    cell = rows[0].split(" | ")[1]
    m = re.search(r"Giao diện: “(.*?)”\s*$", cell)
    assert m, f"mvp mục 8: thiếu câu “Giao diện:” ở dòng not_receiving: {cell!r}"
    doc = m.group(1).replace("*PC-Nha*", "{name}")
    assert result_message(not_receiving_finished(), "PC-Nha") == doc.format(name="PC-Nha")


def test_send_bubble_not_receiving_shows_new_sentence_and_resend(qtbot):
    """Bong bóng gửi bị `not_receiving`: câu 14.14 trên bong bóng, “Gửi lại” vẫn hiện."""
    from teleport.app.devices import DeviceView
    from teleport.ui import texts
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel

    devices = DeviceListModel()
    devices.reset(
        [
            DeviceView(
                device_id=PEER_ID,
                name="PC-Nha",
                os="windows",
                paired=True,
                online=True,
                connected=True,
                addresses=("192.0.2.7",),
                port=47474,
            )
        ]
    )
    view = ChatView(devices)
    view._test_devices = devices  # type: ignore[attr-defined]
    qtbot.addWidget(view)
    view.set_device_names({PEER_ID: "PC-Nha"})
    view.set_device(PEER_ID)
    view.show()
    f = not_receiving_finished()
    view.handle(f)
    view.set_resendable(f.info.key, True)
    bubble = view.row(f.info.key)
    assert bubble.detail_label.text() == GUI_NOT_RECEIVING_VI.format(name=texts.peer_label("PC-Nha", PEER_ID))
    assert bubble.resend_button.isVisibleTo(view)
