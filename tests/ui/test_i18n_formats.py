"""Số và thông báo khi đổi ngôn ngữ (spec D6 mục 2.7, 5.6).

- `texts.format_size` / `texts.format_duration` / `texts.percent`: tiếng Việt ra **y hệt**
  `core.display.format_size` / `format_duration` (CLI vẫn dùng `core`), English đúng dạng (“2.1 MB”,
  “1 byte”, “512 bytes”, “2 min 10 sec”, “42.0%”).
- `InfoBar.show_message` và `ChatView.show_notice` (luồng chat, spec B3): hàm → đổi ngôn ngữ thì câu
  dịch lại; chuỗi → bị ẩn; đổi en → vi liền nhau trước khi Qt xử lý sự kiện → câu vẫn còn (ngôn ngữ
  cuối = ngôn ngữ lúc hiện).
- `DiscoverPage`: lỗi địa chỉ IP đang hiện thì đổi ngôn ngữ hiện lại lỗi bằng ngôn ngữ mới.
- Ô số ngày của Cài đặt: hậu tố theo số (“ ngày”; English “ day” / “ days”); ô tốc độ dùng dấu thập
  phân của ngôn ngữ giao diện.

Module `teleport.ui.*` chỉ import bên trong test. Fixture `ui_language_reset` (conftest) đưa ngôn ngữ về
chuỗi nguồn sau mỗi test.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from teleport.core.display import format_duration as core_format_duration
from teleport.core.display import format_size as core_format_size

KB, MB, GB, TB = 1024, 1024**2, 1024**3, 1024**4

SIZES = [
    0,
    1,
    2,
    512,
    1023,
    1024,
    1025,
    1075,  # 1,0 KB (1,05 làm tròn nửa lên)
    1076,
    10 * KB - 1,
    100 * KB - 52,  # chạm 100,0 → số nguyên
    100 * KB,
    380 * KB,
    1023 * KB,
    1024 * KB - 1,  # làm tròn chạm 1024 KB → 1,0 MB
    1024 * KB - 512,
    MB,
    int(2.1 * MB),
    int(85.24 * MB),
    int(99.95 * MB),
    int(99.94 * MB),
    GB - 1,
    GB,
    4 * GB,
    int(8.04 * GB),
    TB - 1,
    TB,
    int(5.2 * TB),
    1500 * TB,
    3 * 2**42,
]

DURATIONS = [
    0,
    0.4,
    0.5,
    1,
    1.49,
    45,
    59.4,
    59.5,
    60,
    61,
    130,
    3599,
    3599.5,
    3600,
    3660,
    3720.4,
    86399,
    86399.5,
    86400,
    86400 + 3 * 3600,
    86400 + 3599,
    2 * 86400 + 3 * 3600 + 59,
    400 * 86400,
    -5,
]


def _english(qapp) -> None:
    from teleport.ui import i18n

    i18n.set_language("en")
    qapp.processEvents()
    assert i18n.current_language() == "en"


def _vietnamese(qapp) -> None:
    from teleport.ui import i18n

    i18n.set_language("vi")
    qapp.processEvents()


# =================================================================================================
# Dung lượng, phần trăm, thời gian
# =================================================================================================


@pytest.mark.parametrize("trim", [False, True])
@pytest.mark.parametrize("size", SIZES)
def test_format_size_vietnamese_equals_core(qapp, size, trim):
    from teleport.ui import texts

    assert texts.format_size(size, trim=trim) == core_format_size(size, trim=trim)


@pytest.mark.parametrize("trim", [False, True])
@pytest.mark.parametrize("size", SIZES)
def test_format_size_english_uses_point_and_plural_bytes(qapp, size, trim):
    from teleport.ui import texts

    _english(qapp)
    vi = core_format_size(size, trim=trim)
    en = texts.format_size(size, trim=trim)
    assert "," not in en
    if size < 1024:
        assert en == (f"{size} byte" if size == 1 else f"{size} bytes")
    else:
        assert en == vi.replace(",", ".")


@pytest.mark.parametrize(
    ("size", "trim", "expected"),
    [
        (1, False, "1 byte"),
        (512, False, "512 bytes"),
        (int(2.1 * MB), False, "2.1 MB"),
        (1024 * KB - 1, False, "1.0 MB"),
        (4 * GB, True, "4 GB"),
        (4 * GB, False, "4.0 GB"),
        (380 * KB, False, "380 KB"),
    ],
)
def test_format_size_english_examples(qapp, size, trim, expected):
    from teleport.ui import texts

    _english(qapp)
    assert texts.format_size(size, trim=trim) == expected


def test_format_size_rejects_like_core(qapp):
    from teleport.ui import texts

    with pytest.raises(ValueError):
        texts.format_size(-1)
    with pytest.raises(TypeError):
        texts.format_size(1.5)  # type: ignore[arg-type]


@pytest.mark.parametrize("seconds", DURATIONS)
def test_format_duration_vietnamese_equals_core(qapp, seconds):
    from teleport.ui import texts

    assert texts.format_duration(seconds) == core_format_duration(seconds)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 sec"),
        (45, "45 sec"),
        (130, "2 min 10 sec"),
        (120, "2 min"),
        (3660, "1 hr 1 min"),
        (86400, "1 day"),
        (2 * 86400 + 3 * 3600, "2 days 3 hr"),
    ],
)
def test_format_duration_english(qapp, seconds, expected):
    from teleport.ui import texts

    _english(qapp)
    assert texts.format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("shown", "total", "vi", "en"), [(42, 100, "42,0%", "42.0%"), (0, 0, "0,0%", "0.0%")]
)
def test_percent_text_follows_language(qapp, shown, total, vi, en):
    from teleport.ui.transfers_view import percent_text

    assert percent_text(shown, total) == vi
    _english(qapp)
    assert percent_text(shown, total) == en


def test_transfer_speed_in_english_has_no_decimal_comma(qapp):
    """Dòng phụ tiến độ English: “42.0% · 85.2 MB/s · 1 min 2 sec left”-dạng, không còn dấu phẩy thập phân."""
    from teleport.ui.transfers_view import progress_text

    # Chỉ các trường `progress_text` đọc (`app.transfers.TransferProgress`).
    update = SimpleNamespace(
        info=SimpleNamespace(total_bytes=100 * MB),
        shown_bytes=42 * MB,
        bytes_per_second=85.2 * MB,
        eta=62.0,
    )
    _english(qapp)
    text = progress_text(update)  # type: ignore[arg-type]
    assert text.startswith("42.0% · 85.2 MB/s · ")
    assert "1 min 2 sec" in text


# =================================================================================================
# Thông báo dịch lại khi đổi ngôn ngữ
# =================================================================================================


def _menu_open():
    from teleport.ui import texts

    return texts.menu_open()


@pytest.fixture
def info_bar(qtbot):
    from teleport.ui.widgets import InfoBar

    bar = InfoBar()
    qtbot.addWidget(bar)
    bar.show()
    qtbot.waitExposed(bar)
    return bar


def test_info_bar_callable_retranslates(qapp, info_bar):
    from teleport.ui.widgets import InfoLevel

    info_bar.show_message(InfoLevel.INFO, _menu_open, _menu_open, action=_menu_open)
    assert info_bar.title == "Mở Teleport"
    _english(qapp)
    assert info_bar.isVisible()
    assert info_bar.title == "Open Teleport" and info_bar.text == "Open Teleport"
    assert info_bar.action_button.text() == "Open Teleport"
    _vietnamese(qapp)
    assert info_bar.isVisible() and info_bar.title == "Mở Teleport"


def test_info_bar_string_is_hidden_on_language_change(qapp, info_bar):
    from teleport.ui.widgets import InfoLevel

    info_bar.show_message(InfoLevel.INFO, _menu_open())
    _english(qapp)
    assert not info_bar.isVisible(), "câu dịch sẵn của ngôn ngữ cũ không được còn"


def test_info_bar_string_kept_when_language_switches_back_before_events(qapp, info_bar):
    from teleport.ui import i18n
    from teleport.ui.widgets import InfoLevel

    info_bar.show_message(InfoLevel.INFO, _menu_open())
    i18n.set_language("en")
    i18n.set_language("vi")  # trước khi Qt xử lý LanguageChange
    qapp.processEvents()
    assert info_bar.isVisible() and info_bar.title == "Mở Teleport"


@pytest.fixture
def chat_view(qtbot):
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel

    view = ChatView(DeviceListModel())
    qtbot.addWidget(view)
    view.show()
    qtbot.waitExposed(view)
    return view


def test_chat_notice_callable_retranslates(qapp, chat_view):
    calls: list[int] = []
    chat_view.show_notice(_menu_open, _menu_open, lambda: calls.append(1))
    assert chat_view.notice_text() == "Mở Teleport"
    _english(qapp)
    assert chat_view.info_bar.isVisible()
    assert chat_view.notice_text() == "Open Teleport"
    assert chat_view.notice_action_text() == "Open Teleport"
    assert chat_view.notice_label.accessibleDescription() == "Open Teleport"
    assert chat_view.notice_action.accessibleName() == "Open Teleport"
    _vietnamese(qapp)
    assert chat_view.notice_text() == "Mở Teleport"
    chat_view.notice_action.click()
    assert calls == [1], "nút hành động vẫn gọi `on_action` sau khi dịch lại"


def test_chat_notice_string_is_hidden_on_language_change(qapp, chat_view):
    changed: list[int] = []
    chat_view.show_notice(_menu_open())
    chat_view.content_changed.connect(lambda: changed.append(1))
    _english(qapp)
    assert not chat_view.info_bar.isVisible()
    assert chat_view.notice_text() == ""
    assert not chat_view.has_content()
    assert changed, "ẩn thông báo là thay đổi nội dung (`content_changed`)"


def test_chat_notice_string_kept_when_language_switches_back_before_events(qapp, chat_view):
    from teleport.ui import i18n

    chat_view.show_notice(_menu_open())
    i18n.set_language("en")
    i18n.set_language("vi")
    qapp.processEvents()
    assert chat_view.info_bar.isVisible()
    assert chat_view.notice_text() == "Mở Teleport"


# =================================================================================================
# DiscoverPage: lỗi địa chỉ IP
# =================================================================================================


@pytest.fixture
def discover(qtbot):
    from teleport.ui.discover import DiscoverPage
    from teleport.ui.radar import DeviceListModel

    model = DeviceListModel()
    page = DiscoverPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    page.resize(980, 660)
    page.show()
    qtbot.waitExposed(page)
    return page


def test_discover_address_error_retranslates(qapp, discover):
    from teleport.ui.radar import invalid_address_text
    from teleport.ui.widgets import InfoLevel

    discover.address_edit.setText("192.0.2")
    assert not discover.submit_address()
    bar = discover.info_bar
    assert bar.level is InfoLevel.ERROR and bar.title == invalid_address_text("192.0.2")
    vietnamese = bar.title
    _english(qapp)
    assert bar.isVisibleTo(discover) and bar.level is InfoLevel.ERROR
    assert bar.title == invalid_address_text("192.0.2") and bar.title != vietnamese
    assert "192.0.2" in bar.title
    # Gõ chữ vẫn ẩn lỗi sau khi đổi ngôn ngữ.
    discover.address_edit.setText("192.0.2.")
    QApplication.processEvents()
    assert not bar.isVisibleTo(discover)


def test_discover_show_info_replaces_address_error(qapp, discover):
    """`show_info` thay lỗi địa chỉ: đổi ngôn ngữ không hiện lại lỗi cũ, gõ chữ không ẩn câu mới."""
    from teleport.ui.widgets import InfoLevel

    discover.address_edit.setText("192.0.2")
    assert not discover.submit_address()
    discover.show_info(_menu_open)
    bar = discover.info_bar
    assert bar.level is InfoLevel.INFO and bar.title == "Mở Teleport"
    _english(qapp)
    assert bar.level is InfoLevel.INFO and bar.title == "Open Teleport"
    discover.address_edit.setText("192.0.2.1")
    QApplication.processEvents()
    assert bar.isVisibleTo(discover) and bar.title == "Open Teleport"


# =================================================================================================
# Cài đặt: ô số
# =================================================================================================


@pytest.fixture
def settings_page(qtbot):
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import SettingsPage

    model = DeviceListModel()
    page = SettingsPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    return page


def test_retention_suffix_follows_value_and_language(qapp, settings_page):
    spin = settings_page.retention_spin
    settings_page.set_retention(1)
    assert spin.suffix() == " ngày"
    _english(qapp)
    assert spin.suffix() == " day"
    settings_page.set_retention(2)
    assert spin.suffix() == " days"
    spin.stepDown()
    assert spin.value() == 1 and spin.suffix() == " day"
    _vietnamese(qapp)
    assert spin.suffix() == " ngày"


def test_rate_spin_decimal_separator_follows_language(qapp, settings_page):
    spin = settings_page.rate_spin
    spin.setValue(2.5)
    assert spin.cleanText() == "2,5"
    _english(qapp)
    assert spin.cleanText() == "2.5"
    assert spin.value() == 2.5


@pytest.mark.parametrize("english", [False, True])
@pytest.mark.parametrize("typed", ["2,5", "2.5"])
def test_rate_spin_accepts_both_decimal_separators(qapp, settings_page, english, typed):
    """Ô tốc độ không có dấu nhóm: gõ “2,5” hay “2.5” đều là 2,5 ở mọi ngôn ngữ, không thành 25."""
    if english:
        _english(qapp)
    spin = settings_page.rate_spin
    spin.setEnabled(True)
    spin.setValue(10.0)
    spin.lineEdit().setText(typed + spin.suffix())
    spin.interpretText()
    assert spin.value() == 2.5


def test_rate_spin_rejects_grouped_thousands(qapp, settings_page):
    """“1,000” không bị đọc thành 1000 (dấu nhóm bị bỏ lặng lẽ): giá trị giữ nguyên."""
    _english(qapp)
    spin = settings_page.rate_spin
    spin.setEnabled(True)
    spin.setValue(10.0)
    spin.lineEdit().setText("1,000" + spin.suffix())
    spin.interpretText()
    assert spin.value() == 10.0


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_format_duration_rejects_non_finite_like_core(qapp, value):
    from teleport.core.display import format_duration as core_format_duration
    from teleport.ui import texts

    with pytest.raises(ValueError):
        core_format_duration(value)
    with pytest.raises(ValueError):
        texts.format_duration(value)
