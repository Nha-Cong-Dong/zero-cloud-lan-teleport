"""Điều phối gửi/nhận file của giao diện (mvp F4, F5): nối danh sách cuộc trò chuyện, luồng chat,
hộp hỏi, node.

`TransfersController` sống trên thread Qt, dựng trong `ui.app.GuiApp` (thuộc tính `transfers`). Nó
giữ luồng chat `ui.chat.ChatView` (`view`, đặt vào `MainWindow.transfers_area` với lề 0), sổ lời mời
`ui.offer.OfferBook` (`offer_book`) và hộp hỏi `OfferPrompts` (`offers`), và chỉ chạm node qua
`NodeHost.call` / `submit` (không bao giờ gọi `Node`
từ thread Qt). Nối dây (spec B3 mục 5): `conversations.current_changed` → `view.set_device` (gọi một
lần lúc dựng với máy đang chọn), `conversations.open_requested` → `view.focus_composer`; nút “Gửi
file” / Ctrl+O của luồng → `pick_and_send`, thả vào luồng → `request_send`, thả khi chưa chọn máy →
thông báo; “Gửi lại” → `resend`; `view.history_needed` → đọc lịch sử.

**Host** (`attach`): mỗi `NodeHost` mới (lần đầu, “Bắt đầu”, “Thử lại”) → xóa mọi luồng, bong bóng
(kể cả tạm), đường dẫn nhớ cho “Gửi lại” và thông báo, đóng hộp hỏi, bỏ việc “ghép rồi gửi” đang
chờ, đặt thư mục nhận (`options.receive_dir` khi là Path tường minh, không thì None tới khi `ui.app`
gọi `set_receive_dir` theo `ReceiveDirController`). Mọi kết quả `call` / `submit` mang theo host
lúc gọi và bị bỏ nếu host đã đổi; `on_node_event` / `on_transfer` chỉ được `ui.app` gọi với sự
kiện của host hiện tại. Lúc `started` (`on_started`): một `call` đọc `(devices(),
transfers.transfers())` → tên máy, trạng thái kết nối, ảnh chụp các lượt (gộp với `TransferResumed`
tới trước, spec hackathon mục 9.7).

**Lời mời nhận file** (spec B4 mục 3.3): `offer_book` là nguồn sự thật duy nhất; hộp hỏi và bong bóng
chỉ phát ý định (`offers.decided`, `view.offer_answer_requested` → `answer` → `book.answer`; ô “Luôn
nhận” của cả hai → `book.set_always`), nên mỗi offer có đúng một quyết định (lần đầu thắng). `on_transfer`
theo thứ tự: đọc `app_active()`; `TransferFinished` → `book.finish` (hộp và panel đóng trước khi bong
bóng vẽ kết quả); `OfferReceived` chưa tự nhận → đọc `offer_in_view(id)` **trước** khi chèn bong bóng,
`view.handle`, `book.add(connected=…)` (theo `window.devices`), thêm được mà không `in_view` thì
`offers.show_prompt`. `offer_in_view(id)` đúng khi cửa sổ chính đang active
(`default_window_active()` cấp module, đọc lúc gọi), đang hiện và không thu nhỏ, RUNNING, ở trang HOME,
đang mở luồng của `id` và luồng đó ở cuối: chỉ khi người dùng thật sự thấy lời mời thì bong bóng mới
đủ, còn lại bật hộp. Cửa sổ thu nhỏ hay bị ẩn (`eventFilter` trên cửa sổ: `WindowStateChange` sang thu
nhỏ, `Hide`) → `on_window_hidden()`: mở hộp cho mọi lời mời PENDING chưa có hộp, theo thứ tự tới.
`book.answered(key, accept, always)` → **một** `host.call(_apply_answer)`: trong cùng khối đồng bộ
trên thread node, lượt không còn (`transfers.get` None) thì thôi; `decide(key, accept)`; `accept`,
`always` và lượt còn ở pha hỏi lúc đọc `get` (status WAITING) thì `set_ask_before_receive(id, False)`
(`KeyError` / `RuntimeError` im lặng). Kết quả về
(host cũ thì bỏ): `decide` lỗi → WARNING (tên loại); lưu “Luôn nhận” lỗi → câu mvp mục 8
`offer.always_save_error_text` ở thanh thông báo (WARNING); lưu được → `always_accept_saved(id)`.
`cancel(key)` của lời mời PENDING là `book.answer(key, False)` (✕ của bong bóng vốn đã ẩn khi có panel).
“Đổi…” của hộp và panel → `change_receive_dir_requested` (`ui.app` mở hộp chọn thư mục);
`set_receive_dir_pending` / `show_receive_dir_error` cập nhật cả hai chỗ. `DeviceConnected` /
`DeviceDisconnected` và ảnh chụp lúc `started` → `book.set_connected`. Host mới và Thoát:
`book.clear()` trước `view.clear()` / `offers.close_all()`, không quyết định (node tự từ chối khi đóng).

**Hoạt động theo máy** (spec B2 mục 2.3; danh sách cuộc trò chuyện ở cột trái): `activity`
(`ui.activity.ActivityTracker`) theo dõi lượt đang chạy và lượt đã kết thúc gần nhất của mỗi máy, rồi
đẩy vào `window.conversations.model.set_activity` mỗi khi một máy đổi. Nguồn: các `TransferUpdate`
của `on_transfer` (sau `view.handle`; mốc là giờ tường `_wall_now()` lúc nhận, seam cấp module đọc
lúc gọi), ảnh chụp `transfers()` lúc `started`, và một `call` thứ hai lúc `started` đọc
`latest_by_device(node.transfers.history())` (rút gọn trên thread node). Kết quả của host cũ hay về
sau `close_all` bị bỏ; lỗi → WARNING (tên loại lỗi). Host mới (`attach`) → xóa hết. Không polling node.
Lượt bị gỡ bong bóng (bước 3 dưới đây) vẫn báo `TransferFinished` cho `activity` để máy hết “đang bận”.

**Lịch sử theo máy** (F5, spec B3 mục 5): `view.history_needed(id)` → `host.call(transfers.history(id))`
khi node của host hiện tại đã `started`; chưa thì nhớ máy đó và gọi đúng một lần trong `on_started`
(nếu vẫn là máy đang mở). Kết quả → `view.load_history` (luồng tự bỏ kết quả tới muộn); host cũ hay
sau `close_all` → bỏ; lỗi → WARNING (tên loại lỗi) và `view.history_failed`.

**Gửi** (`request_send(device_id, paths)`, từ kéo thả, nút “Gửi file” / Ctrl+O của luồng, menu
“Gửi file…” của cột trái, hay menu con “Gửi file…” của khay qua `pick_and_send`): thread Qt không `stat`
file người dùng (D7); việc kiểm file do `transfers.send` làm trên `DiskExecutor` của node.
1. Danh sách rỗng → bỏ qua; `send.selection_error` (quá `MAX_OFFER_FILES` file, trùng) → thông báo.
2. Thiết bị chưa ghép đôi → “ghép rồi gửi” (từ B2 giao diện không còn đường nào gửi tới máy chưa ghép
   đôi: danh sách cuộc trò chuyện chỉ có máy đã ghép đôi; phần này gỡ ở B2c): nhớ một việc chờ
   `(device_id, paths)` và phát `pair_needed(DeviceView)` (luồng ghép đôi nối vào). Chưa ai nối
   `pair_needed` → không giữ việc chờ, hiện câu mvp mục 8 “Chưa ghép đôi với *X*…”. Đã có việc chờ
   → câu mvp mục 8 “Đang chờ ghép đôi với *A* để gửi file…” (*A* là máy **đang chờ ghép**).
   `on_pairing_finished(peer_device_id, paired)` kết thúc việc chờ: khớp `peer_device_id` (không
   theo `session_id`, spec mục 9.2) và `paired` → gửi; không ghép được (`paired` False, với
   `peer_device_id` khớp hay None) → bỏ việc chờ (luồng ghép đôi tự báo lỗi). Lưới an toàn: luồng
   ghép đôi không báo lại thì việc chờ tự bỏ, im lặng, sau `PENDING_PAIR_TIMEOUT` giây (`QTimer` một
   lần; `attach`, `close_all` và lúc việc chờ kết thúc thì dừng hẹn).
3. Kết nối đang bị chặn (`ConnectFailed` gần nhất trong `window.devices` là REJECTED,
   IDENTITY_MISMATCH, VERSION_MISMATCH; spec mục 10 A2) → câu mvp mục 8 (mức WARNING), không tạo
   lượt. Thanh thông báo kèm nút:
   REJECTED → “Ghép đôi lại” (phát `repair_requested(DeviceView)`, luồng ghép đôi nối);
   IDENTITY_MISMATCH → “Hủy ghép đôi” (phát `conversations.unpair_requested(device_id)`, luồng ghép
   đôi hỏi xác nhận). Chỉ chưa kết nối thì vẫn gửi: bong bóng hiện “Đang chờ kết nối tới X…”.
   Lần mở kết nối do chính lượt đó gây ra mà bị chặn (`on_node_event` nhận `ConnectFailed` chặn,
   lần đầu `window.devices` biết) → mọi **lượt mới** gửi tới máy đó bị gỡ bong bóng và hủy (không treo
   “Đang nối lại…” tới hạn nối lại), rồi hiện câu và nút như trên. Lượt mới: lượt gửi mà lúc bong bóng xuất
   hiện `window.devices` báo chưa kết nối, và chưa có `DeviceConnected` nào của máy đó từ đó tới giờ,
   tức `offer` chưa từng đi. Lượt đã gửi `offer` (lượt dở) giữ nguyên: bị từ chối khi còn lượt dở thì
   `core.links` vẫn hẹn thử lại (docstring `core/links.py`), lỗi tạm trông giống bị từ chối.
   Đánh đổi: lỗi tạm đó (máy kia đang thoát hay khởi động lại) cũng gỡ lượt mới, người dùng gửi lại;
   lượt bị gỡ vào lịch sử là `CANCELLED_USER` (như bấm “Hủy”).
4. `view.add_pending` (bong bóng tạm “Đang chuẩn bị…”) rồi `host.submit(transfers.send)`; kết quả về
   thì luôn `view.remove_pending` trước. Lỗi → `send.send_error_message` (None thì im lặng; mức
   ERROR). Thành công (`TransferKey`): bong bóng của lượt tự hiện qua `TransferStatusChanged`; lượt
   không bị gỡ và còn bong bóng thì nhớ đường dẫn cho “Gửi lại” (`_sources`, chỉ trong phiên, trần
   `SOURCES_MAX_TRANSFERS` lượt và `SOURCES_MAX_PATHS` đường dẫn, bỏ mục cũ nhất; bỏ khi lượt xong
   DONE / UNPAIRED hay bị gỡ vì bị chặn; không nhớ khi `send` trả về sau lúc lượt đã xong như vậy) và
   `view.set_resendable(key, True)`. Bấm “Gửi lại”
   (`resend`): quên đường dẫn của lượt đó, tắt nút, rồi `request_send` lại đúng các đường dẫn.

“Gửi file…” (`pick_and_send`): `QFileDialog` cha là cửa sổ chính, `ExistingFiles`, mở bằng `open()`
(không `exec()`), dựng qua seam cấp module `file_dialog_factory` (đọc lúc gọi).

**Thông báo**: lỗi chọn gửi hiện ở thanh thông báo của luồng chat (`view.show_notice`, mức ERROR),
không hộp chặn. Khay: có `offer` chờ trả lời → `texts.offer_waiting()`, **chỉ khi app không được dùng**
(`app_active()` False: không cửa sổ nào của app đang active; đọc trước khi mở hộp hỏi, vì hộp mới
mở có thể giành active); nhận xong một lượt → câu mvp mục 8 “Đã nhận *n* file từ *X*.” (luôn
hiện; một file: tên file thật đã lưu, lấy từ `FileDone.path.name` cuối, có thể là `ten (1).ext`;
chưa có thì tên trong `offer`), kèm `folder` để bấm bóng thoại mở thư mục: thư mục cha của file đã
lưu cuối, không có thì thư mục nhận hiện tại (`set_receive_dir`). Tên máy kia qua
`texts.peer_label`, tên file qua `texts.file_label`; bóng thoại khay là chữ thuần.

**Tóm tắt cho cửa sổ chính và khay**: mỗi `view.content_changed` (và một lần lúc dựng) gọi
`MainWindow.set_transfer_summary(has_content(), active_count())` (huy hiệu “Đang truyền n lượt”, màn
rỗng của khung phải, nút “Mở thư mục” cuối chuỗi Tab) và `Tray.set_activity(active_count())` (dấu
biểu tượng và tooltip khay, spec D4).

**Lượt gửi dở** (`active_sends()`): tên hiển thị các máy đang có lượt gửi chưa kết thúc, theo các
bong bóng của phiên trong luồng chat (`view.rows()`, không gọi node); `ui.app` dùng để hỏi lại trước
khi Thoát.

Chuỗi qua `QCoreApplication.translate("Transfers", ...)` lúc dùng.
"""

from __future__ import annotations

import functools
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import cast

from PySide6.QtCore import QCoreApplication, QEvent, QMetaMethod, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from teleport.app.devices import DeviceView
from teleport.app.node import (
    ConnectFailed,
    DeviceConnected,
    DeviceDisconnected,
    DeviceOffline,
    DeviceOnline,
    Node,
    NodeEvent,
)
from teleport.app.transfers import (
    Direction,
    FileDone,
    OfferReceived,
    TransferFinished,
    TransferInfo,
    TransferKey,
    TransferResult,
    TransferStatus,
    TransferUpdate,
)
from teleport.core import pairing
from teleport.core.links import ConnectResult
from teleport.ui import send, texts
from teleport.ui.activity import ActivityTracker, FinishedTransfer, latest_by_device
from teleport.ui.bridge import NodeHost
from teleport.ui.chat import ChatView
from teleport.ui.main_window import MainWindow, Page, ViewState
from teleport.ui.offer import OfferBook, OfferPhase, OfferPrompts, always_save_error_text
from teleport.ui.tray import Tray
from teleport.ui.widgets import InfoLevel

# Seam: dựng hộp chọn file (test thay bằng hàm trả hộp giả). Nhận cha (cửa sổ chính).
file_dialog_factory: Callable[[QWidget | None], QFileDialog] = QFileDialog
# Seam: giờ tường (giây epoch) làm mốc hoạt động của lượt; đọc lúc gọi.
_wall_now: Callable[[], float] = time.time

log = logging.getLogger(__name__)

_DEVICE_EVENTS = (DeviceOnline, DeviceOffline, DeviceConnected, DeviceDisconnected, ConnectFailed)


# Lưới an toàn của “ghép rồi gửi”: việc chờ tự bỏ (im lặng) sau từng này giây nếu luồng ghép đôi
# không báo lại. Dài hơn phiên ghép đôi dài nhất ở vai khởi xướng (chờ `pair_commit` và `pair_reveal`,
# người dùng quyết định, thời gian ân hạn), cộng `_PENDING_PAIR_MARGIN` cho việc nối kết nối và hộp
# của luồng ghép đôi. Đọc lúc đặt hẹn (test chỉnh được).
_PENDING_PAIR_MARGIN = 60.0
PENDING_PAIR_TIMEOUT = (
    2 * pairing.MESSAGE_TIMEOUT + pairing.CONFIRM_TIMEOUT + pairing.CONFIRM_GRACE + _PENDING_PAIR_MARGIN
)
# Trần bộ nhớ đường dẫn nguồn cho “Gửi lại” (chỉ trong phiên, spec B3 mục 9.1).
SOURCES_MAX_TRANSFERS = 256
SOURCES_MAX_PATHS = 4096
# Kết quả không bao giờ có “Gửi lại”: đường dẫn nguồn của lượt đó không cần nhớ.
_NO_RESEND = (TransferResult.DONE, TransferResult.UNPAIRED)


def default_app_active() -> bool:
    """App đang được dùng: một cửa sổ của app (cửa sổ chính hay một hộp) đang active."""
    return QApplication.activeWindow() is not None


def default_window_active() -> bool:
    """Cửa sổ chính của app đang active (không phải một hộp); seam cho test, đọc lúc gọi."""
    return isinstance(QApplication.activeWindow(), MainWindow)


class _AlwaysFailed:
    """Kết quả của `_apply_answer`: đã `decide` nhưng không lưu được “Luôn nhận” (`error`)."""

    __slots__ = ("error",)

    def __init__(self, error: BaseException) -> None:
        self.error = error


_ALWAYS_SAVED = "always_saved"  # kết quả của `_apply_answer`: đã lưu “Luôn nhận”


def _apply_answer(node: Node, key: TransferKey, accept: bool, always: bool) -> object:
    """Chạy trên thread node (một khối đồng bộ): gửi quyết định, rồi lưu “Luôn nhận” khi cần.

    Lượt không còn → None. Lỗi của `decide` ném ra (host trả về làm kết quả). “Luôn nhận” chỉ lưu khi
    lượt còn ở pha hỏi người dùng lúc `decide`. Với lượt nhận, WAITING của engine là pha CHECKING hay
    ASKING; CHECKING không xảy ra với key đã có hộp hay bong bóng (engine chỉ phát `OfferReceived`
    khi vào ASKING, và kiểm lại chỗ trống lúc đổi thư mục giữ nguyên ASKING), nên WAITING ở đây là
    ASKING; nhận rồi là TRANSFERRING trở đi. ASKING mà đang kiểm lại chỗ trống (`rechecking`): engine
    chỉ ghi nhận Nhận và có thể từ chối sau nếu thiếu chỗ, nhưng vẫn lưu “Luôn nhận” — chấp nhận,
    vì đó đúng là ý người dùng với máy này. Lượt đã qua pha hỏi (đã trả lời ở chỗ khác) → `decide`
    bị engine bỏ qua và không lưu gì. Lỗi lưu
    cài đặt: `KeyError` (máy vừa bị hủy ghép đôi) / `RuntimeError` (node đang đóng) → None, im lặng;
    lỗi khác → `_AlwaysFailed`. Lưu được → `_ALWAYS_SAVED`.
    """
    info = node.transfers.get(key)
    if info is None:
        return None
    asking = key.direction is Direction.RECEIVE and info.status is TransferStatus.WAITING
    node.transfers.decide(key, accept)
    if not (accept and always and asking):
        return None
    try:
        node.set_ask_before_receive(key.device_id, False)
    except (KeyError, RuntimeError):
        return None
    except Exception as e:
        return _AlwaysFailed(e)
    return _ALWAYS_SAVED


def pair_wait_text(peer: str) -> str:
    """mvp mục 8, “Giao diện: gửi tiếp khi đang chờ ghép đôi để gửi”. `peer`: máy **đang chờ ghép**."""
    return texts.fill(
        QCoreApplication.translate(
            "Transfers", "Đang chờ ghép đôi với {name} để gửi file. Hãy chờ xong rồi gửi tiếp."
        ),
        name=peer,
    )


def received_text(info: TransferInfo, peer: str, saved_name: str | None = None) -> str:
    """Bóng thoại khay khi nhận xong một lượt (mvp mục 8). `peer` đã qua `peer_label`.

    Một file: tên file thật đã lưu (`saved_name`, từ `FileDone.path.name`; có thể đã thành
    `ten (1).ext` để không ghi đè) khi có, không thì tên trong `offer`.
    """
    t = QCoreApplication.translate
    if len(info.files) == 1 and (saved_name or info.names):
        name = saved_name or info.names[0]
        return texts.fill(t("Transfers", "Đã nhận {file} từ {name}."), file=texts.file_label(name), name=peer)
    return texts.fill(t("Transfers", "Đã nhận %n file từ {name}.", None, len(info.files)), name=peer)


def repair_action_text() -> str:
    """Nút của thông báo “*X* từ chối kết nối từ máy này” (mvp mục 8, “Máy kia từ chối máy này”)."""
    return QCoreApplication.translate("Transfers", "Ghép đôi lại")


def unpair_action_text() -> str:
    """Nút của thông báo “*X* đang dùng một danh tính khác…” (mvp mục 8, “Danh tính thiết bị thay đổi”)."""
    return QCoreApplication.translate("Transfers", "Hủy ghép đôi")


def pick_title(peer: str) -> str:
    """Tiêu đề hộp chọn file của “Gửi file…”. `peer` đã qua `peer_label`."""
    return texts.fill(QCoreApplication.translate("Transfers", "Chọn file gửi tới {name}"), name=peer)


class TransfersController(QObject):
    """Gửi/nhận file trên thread Qt: cột trái → node, sự kiện lượt → luồng chat, hoạt động theo
    máy, hộp hỏi, khay."""

    pair_needed = Signal(object)  # DeviceView: chưa ghép đôi, cần ghép trước khi gửi
    repair_requested = Signal(object)  # DeviceView: nút “Ghép đôi lại” (máy kia từ chối máy này)
    always_accept_saved = Signal(str)  # device_id: đã lưu “Luôn nhận” (trang Cài đặt đọc lại)
    change_receive_dir_requested = Signal()  # “Đổi…” của hộp hỏi hay panel lời mời

    def __init__(
        self,
        window: MainWindow,
        tray: Tray,
        parent: QObject | None = None,
        *,
        app_active: Callable[[], bool] | None = None,
    ) -> None:
        """`app_active`: app đang được dùng không (bóng thoại offer chỉ hiện khi False).

        Mặc định `default_app_active` của module (đọc lúc gọi, test vá được).
        """
        super().__init__(parent)
        self._window = window
        self._tray = tray
        self._app_active = app_active if app_active is not None else lambda: default_app_active()
        self._host: NodeHost | None = None
        self._names: dict[str, str | None] = {}
        self._offer_names: dict[TransferKey, str | None] = {}
        self._saved_paths: dict[TransferKey, Path] = {}  # file thật đã lưu cuối (`FileDone.path`)
        self._pending: tuple[str, list[Path]] | None = None  # “ghép rồi gửi” đang chờ
        self._unoffered: set[TransferKey] = set()  # lượt gửi mới: `offer` chưa từng đi (bước 3)
        self._dropped: set[TransferKey] = set()  # bong bóng đã gỡ, đang chờ `TransferFinished` của lần hủy
        # Đường dẫn nguồn của lượt gửi trong phiên, cho “Gửi lại” (cũ nhất trước; trần `SOURCES_*`).
        self._sources: OrderedDict[TransferKey, tuple[Path, ...]] = OrderedDict()
        self._started: NodeHost | None = None  # host đã `started` (đọc lịch sử được)
        self._history_waiting: str | None = None  # máy chờ đọc lịch sử tới lúc `started`
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.timeout.connect(self._drop_pending)
        self._dialog: QFileDialog | None = None
        self._closing = False  # `close_all` đã chạy: bỏ kết quả đọc lịch sử về muộn
        self.activity = ActivityTracker()
        self._receive_dir: Path | None = None

        self.offer_book = OfferBook(self)
        self.view = ChatView(window.devices, window.transfers_area)
        area_layout = window.transfers_area.layout()
        if area_layout is not None:
            area_layout.setContentsMargins(0, 0, 0, 0)  # luồng chat tự lo lề
            area_layout.addWidget(self.view)
        self.offers = OfferPrompts(self.offer_book, parent=self)
        self.view.set_offer_book(self.offer_book)

        conversations = window.conversations
        conversations.send_files_requested.connect(self.request_send)
        conversations.pick_files_requested.connect(self.pick_and_send)
        conversations.drop_hint_requested.connect(self.view.show_notice)
        conversations.current_changed.connect(self.view.set_device)
        conversations.open_requested.connect(self.view.focus_composer)
        self.view.cancel_requested.connect(self.cancel)
        self.view.resend_requested.connect(self.resend)
        self.view.pick_files_requested.connect(self.pick_and_send)
        self.view.send_files_requested.connect(self.request_send)
        self.view.drop_hint_requested.connect(self.view.show_notice)
        self.view.history_needed.connect(self._on_history_needed)
        current = conversations.current_device()
        self.view.set_device(None if current is None else current.device_id)
        self.view.content_changed.connect(self._update_summary)
        self.offers.decided.connect(self.answer)
        self.view.offer_answer_requested.connect(self.answer)
        self.offers.always_toggled.connect(self.offer_book.set_always)
        self.view.offer_always_toggled.connect(self.offer_book.set_always)
        self.offers.change_dir_requested.connect(self.change_receive_dir_requested)
        self.view.change_receive_dir_requested.connect(self.change_receive_dir_requested)
        self.offer_book.answered.connect(self._send_answer)
        window.installEventFilter(self)
        self._update_summary()

    # ---- Vòng đời host -----------------------------------------------------------------------

    @property
    def host(self) -> NodeHost | None:
        return self._host

    @property
    def pending(self) -> tuple[str, list[Path]] | None:
        """Việc “ghép rồi gửi” đang chờ: (device_id, các đường dẫn), hay None."""
        return self._pending

    def attach(self, host: NodeHost | None) -> None:
        """Dùng `host` mới: xóa lời mời, lượt, hoạt động và thông báo, đóng hộp hỏi, bỏ việc chờ, đặt thư
        mục nhận."""
        self._host = host
        self.offer_book.clear()  # trước `view.clear()` và `offers.close_all()` (spec B4 mục 11.2)
        self.view.clear()
        self.activity.clear()
        self._window.conversations.model.clear_activity()
        self.offers.close_all()
        self._drop_pending()
        self._names = {}
        self._offer_names = {}
        self._saved_paths = {}
        self._unoffered = set()
        self._dropped = set()
        self._sources = OrderedDict()
        self._started = None
        self._history_waiting = None
        self.view.set_device_names(self._names)
        receive_dir = None if host is None else host.options.receive_dir
        self.set_receive_dir(receive_dir if isinstance(receive_dir, Path) else None)

    @property
    def receive_dir(self) -> Path | None:
        """Thư mục nhận hiện tại (hộp hỏi, câu lỗi, thư mục dự phòng của bóng thoại “Đã nhận…”)."""
        return self._receive_dir

    def set_receive_dir(self, path: Path | None) -> None:
        """Đặt thư mục nhận cho hộp hỏi (kể cả hộp đang mở), luồng chat và bóng thoại khay."""
        self._receive_dir = path
        self.offers.set_receive_dir(path)
        self.view.set_receive_dir(path)

    def set_receive_dir_pending(self, path: Path | None) -> None:
        """Thư mục nhận đang đổi sang (None: không đổi): hộp hỏi và panel lời mời tắt “Nhận”."""
        self.offers.set_receive_dir_pending(path)
        self.view.set_receive_dir_pending(path)

    def show_receive_dir_error(self, text: texts.Text | None) -> None:
        """Lỗi đổi thư mục nhận ở các hộp hỏi **đang mở** (hộp mở sau không có); None → gỡ. Hàm dựng
        câu thì đổi ngôn ngữ dịch lại."""
        self.offers.set_dir_error(text)

    def on_started(self, host: NodeHost) -> None:
        """Node của `host` đã chạy: đọc một ảnh chụp thiết bị và lượt truyền, và lượt gần nhất của mỗi máy
        trong lịch sử."""
        if host is not self._host:
            return
        self._started = host
        host.call(
            lambda node: (node.devices(), node.transfers.transfers()),
            functools.partial(self._on_snapshot, host),
        )
        host.call(
            lambda node: latest_by_device(node.transfers.history()),
            functools.partial(self._on_history, host),
        )
        # Lịch sử của luồng đang mở mà `history_needed` tới trước `started` (gọi đúng một lần).
        waiting, self._history_waiting = self._history_waiting, None
        if waiting is not None and waiting == self.view.device_id():
            self._load_thread_history(host, waiting)

    def close_all(self) -> None:
        """App đang thoát: đóng hộp hỏi và hộp chọn file, bỏ việc chờ."""
        self._closing = True
        self.offer_book.clear()  # trước `offers.close_all()`: không quyết định gì
        self.offers.close_all()
        self._drop_pending()
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    # ---- Sự kiện của node (đã về thread Qt, của host hiện tại) --------------------------------

    def on_node_event(self, event: NodeEvent) -> None:
        if not isinstance(event, _DEVICE_EVENTS):
            return
        view = event.device
        if not isinstance(view, DeviceView):
            return
        if isinstance(event, DeviceConnected):
            self.view.set_connected(view.device_id, True)
            self.offer_book.set_connected(view.device_id, True)
            self._unoffered = {k for k in self._unoffered if k.device_id != view.device_id}
        elif isinstance(event, DeviceDisconnected):
            self.view.set_connected(view.device_id, False)
            self.offer_book.set_connected(view.device_id, False)
        if self._names.get(view.device_id) != view.name:
            self._names[view.device_id] = view.name
            self.view.set_device_names(self._names)
        if isinstance(event, ConnectFailed):
            self._on_connect_failed(view, event.result)

    def active_sends(self) -> list[str]:
        """Tên hiển thị (`texts.peer_label`) các máy đang có lượt gửi chưa kết thúc, theo thứ tự hàng.

        Mỗi máy một lần (theo `device_id`). Chỉ đọc bong bóng của phiên (`view.rows()`), không gọi node.
        """
        model = self._window.devices
        seen: set[str] = set()
        names: list[str] = []
        for row in self.view.rows():
            key = row.key
            if key.direction is not Direction.SEND or row.finished or key.device_id in seen:
                continue
            seen.add(key.device_id)
            names.append(self._peer(key.device_id, model.device(key.device_id)))
        return names

    def on_transfer(self, update: TransferUpdate) -> None:
        key = _update_key(update)
        if key in self._dropped:
            # Lượt mới đã gỡ thẻ vì bị chặn (bước 3): bỏ mọi sự kiện, tới `TransferFinished` của lần hủy;
            # sự kiện đó vẫn vào `activity` để máy hết “đang bận”.
            if isinstance(update, TransferFinished):
                self._dropped.discard(key)
                self._offer_names.pop(key, None)
                self._track(update)
            return
        if (
            key is not None
            and key.direction is Direction.SEND
            and not isinstance(update, TransferFinished)
            and self.view.row(key) is None
        ):
            device = self._window.devices.device(key.device_id)
            if device is None or not device.connected:
                self._unoffered.add(key)
        # Đọc trước khi mở hộp hỏi: hộp mới mở có thể giành active.
        active = isinstance(update, OfferReceived) and self._app_active()
        if isinstance(update, TransferFinished):
            self.offer_book.finish(update.info.key)  # hộp và panel đóng trước khi bong bóng vẽ kết quả
        asking = isinstance(update, OfferReceived) and not update.auto_accepted
        # Đọc trước khi chèn bong bóng: luồng còn đang ở cuối.
        in_view = asking and isinstance(update, OfferReceived) and self.offer_in_view(update.key.device_id)
        self.view.handle(update)
        if asking and isinstance(update, OfferReceived):
            device = self._window.devices.device(update.key.device_id)
            connected = True if device is None else device.connected
            if self.offer_book.add(update, connected=connected) and not in_view:
                self.offers.show_prompt(update.key)
        self._track(update)
        if isinstance(update, OfferReceived):
            self._offer_names[update.key] = update.device_name
            if not update.auto_accepted and not active:
                self._tray.show_message(texts.offer_waiting())
        elif isinstance(update, FileDone):
            if update.key.direction is Direction.RECEIVE and update.path is not None and update.path.name:
                self._saved_paths[update.key] = update.path
        elif isinstance(update, TransferFinished):
            key = update.info.key
            self._unoffered.discard(key)
            if update.result in _NO_RESEND:
                self._sources.pop(key, None)
            offer_name = self._offer_names.pop(key, None)
            saved_path = self._saved_paths.pop(key, None)
            if update.result is TransferResult.DONE and key.direction is Direction.RECEIVE:
                name = self._names.get(key.device_id) or offer_name
                if saved_path is not None:
                    folder: Path | None = saved_path.parent
                else:
                    folder = self._receive_dir
                self._tray.show_message(
                    received_text(
                        update.info,
                        texts.peer_label(name, key.device_id),
                        None if saved_path is None else saved_path.name,
                    ),
                    folder=folder,
                )

    # ---- Gửi ---------------------------------------------------------------------------------

    def request_send(self, device_id: str, paths: Sequence[Path]) -> None:
        """Gửi `paths` tới `device_id` (kéo thả, “Gửi file…”). Không chạm đĩa trên thread Qt."""
        paths = list(paths)
        if not paths:
            return
        error = send.selection_error(paths)
        if error is not None:
            self.view.show_notice(error, level=InfoLevel.ERROR)
            return
        model = self._window.devices
        view = model.device(device_id)
        peer = self._peer(device_id, view)
        if view is not None and not view.paired:
            if self._pending is not None:
                waiting_id = self._pending[0]
                self.view.show_notice(pair_wait_text(self._peer(waiting_id, model.device(waiting_id))))
                return
            if not view.online:
                return  # không ghép đôi được với máy không trong mạng
            if not self.isSignalConnected(QMetaMethod.fromSignal(self.pair_needed)):
                # Chưa có luồng ghép đôi nào nối vào: không giữ việc chờ (sẽ kẹt mãi).
                self.view.show_notice(send.not_paired_message(peer))
                return
            self._pending = (device_id, paths)
            self._pending_timer.start(max(0, int(PENDING_PAIR_TIMEOUT * 1000)))
            self.pair_needed.emit(view)
            return
        failure = model.failure(device_id)
        if failure is not None:
            blocked = send.blocked_message(failure, peer)
            if blocked is not None:
                self._show_blocked(blocked, failure, device_id, view)
                return
        self._submit_send(device_id, paths, peer)

    def on_pairing_finished(self, peer_device_id: str | None, paired: bool) -> None:
        """Kết quả ghép đôi (luồng ghép đôi gọi): khớp việc “ghép rồi gửi” đang chờ theo `peer_device_id`.

        Khớp và `paired` → gửi; `paired` False mà `peer_device_id` khớp hay None (phiên dừng trước khi
        biết máy kia) → bỏ việc chờ. Ghép được với máy khác → giữ việc chờ (hẹn bỏ vẫn chạy).
        """
        pending = self._pending
        if pending is None:
            return
        device_id, paths = pending
        if paired and peer_device_id == device_id:
            self._drop_pending()
            self._submit_send(device_id, paths, self._peer(device_id, self._window.devices.device(device_id)))
        elif not paired and (peer_device_id is None or peer_device_id == device_id):
            self._drop_pending()

    def pick_and_send(self, device_id: str) -> None:
        """“Gửi file…”: mở hộp chọn nhiều file (không chặn), chọn xong thì `request_send`."""
        old, self._dialog = self._dialog, None
        if old is not None:
            old.close()
            old.deleteLater()
        view = self._window.devices.device(device_id)
        dialog = file_dialog_factory(self._window)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setWindowTitle(pick_title(self._peer(device_id, view)))
        dialog.filesSelected.connect(
            lambda files, i=device_id: self.request_send(i, [Path(f) for f in files if f])
        )
        dialog.finished.connect(lambda _result, d=dialog: self._dialog_finished(d))
        self._dialog = dialog
        dialog.open()

    def resend(self, key: TransferKey) -> None:
        """“Gửi lại” của bong bóng `key`: gửi lại đúng các đường dẫn đã nhớ, một lần."""
        paths = self._sources.pop(key, None)
        self.view.set_resendable(key, False)
        if paths is not None:
            self.request_send(key.device_id, list(paths))

    # ---- Hủy, trả lời lời mời ------------------------------------------------------------------

    def cancel(self, key: TransferKey) -> None:
        """Hủy lượt `key`; lời mời đang chờ trả lời thì là “Từ chối” qua book (một quyết định)."""
        entry = self.offer_book.entry(key)
        if entry is not None and entry.phase is OfferPhase.PENDING:
            self.offer_book.answer(key, False)
            return
        host = self._host
        if host is not None:
            host.call(lambda node: node.transfers.cancel(key), functools.partial(self._log_error, "hủy lượt"))

    def answer(self, key: TransferKey, accept: bool) -> None:
        """Ý định trả lời lời mời `key` (hộp, bong bóng): chỉ lần đầu với lời mời PENDING có hiệu lực."""
        self.offer_book.answer(key, accept)

    def decide(self, key: TransferKey, accept: bool) -> None:
        """Bí danh cũ của `answer` (đi qua book)."""
        self.answer(key, accept)

    def offer_in_view(self, device_id: str) -> bool:
        """Người dùng đang thấy cuối luồng của `device_id` (lời mời từ máy đó chỉ cần bong bóng)."""
        window = self._window
        return bool(
            default_window_active()
            and window.isVisible()
            and not window.isMinimized()
            and window.view_state is ViewState.RUNNING
            and window.current_page is Page.HOME
            and self.view.device_id() == device_id
            and self.view.is_pinned(device_id)
        )

    def on_window_hidden(self) -> None:
        """Cửa sổ bị ẩn hay thu nhỏ: mở hộp cho mọi lời mời PENDING chưa có hộp, theo thứ tự tới."""
        if self._closing:
            return
        for key in self.offer_book.pending_keys():
            self.offers.show_prompt(key)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._window:
            kind = event.type()
            if kind == QEvent.Type.Hide or (
                kind == QEvent.Type.WindowStateChange
                and self._window.windowState() & Qt.WindowState.WindowMinimized
            ):
                self.on_window_hidden()
        return super().eventFilter(watched, event)

    def _send_answer(self, key: TransferKey, accept: bool, always: bool) -> None:
        """`book.answered`: một `host.call` gửi quyết định (và lưu “Luôn nhận” khi cần)."""
        host = self._host
        if host is None:
            return
        host.call(
            functools.partial(_apply_answer, key=key, accept=bool(accept), always=bool(always)),
            functools.partial(self._on_answer_done, host, key),
        )

    def _on_answer_done(self, host: NodeHost, key: TransferKey, result: object) -> None:
        if host is not self._host:
            return
        if isinstance(result, BaseException):
            log.warning("không trả lời được lời mời nhận file: %s", type(result).__name__)
        elif isinstance(result, _AlwaysFailed):
            log.warning("không lưu được “Luôn nhận”: %s", type(result.error).__name__)
            peer = self._peer(key.device_id, self._window.devices.device(key.device_id))
            self.view.show_notice(functools.partial(always_save_error_text, peer), level=InfoLevel.WARNING)
        elif result == _ALWAYS_SAVED:
            self.always_accept_saved.emit(key.device_id)

    # ---- Bên trong ---------------------------------------------------------------------------

    def _track(self, update: TransferUpdate) -> None:
        """Đưa `update` vào `activity`; máy đổi thì đẩy hoạt động mới vào danh sách cuộc trò chuyện."""
        device_id = self.activity.handle(update, _wall_now())
        if device_id is not None:
            self._push_activity((device_id,))

    def _push_activity(self, device_ids: Iterable[str]) -> None:
        model = self._window.conversations.model
        for device_id in device_ids:
            model.set_activity(device_id, self.activity.get(device_id))

    def _update_summary(self) -> None:
        """Báo cửa sổ chính luồng chat có gì không và số lượt chưa kết thúc (huy hiệu, màn rỗng), và
        số lượt đó cho khay (dấu và tooltip “Đang truyền n lượt”)."""
        active = self.view.active_count()
        self._window.set_transfer_summary(self.view.has_content(), active)
        self._tray.set_activity(active)

    def _peer(self, device_id: str, view: DeviceView | None) -> str:
        name = view.name if view is not None else self._names.get(device_id)
        return texts.peer_label(name, device_id)

    def _drop_pending(self) -> None:
        """Bỏ việc “ghép rồi gửi” đang chờ (im lặng) và hẹn tự bỏ của nó."""
        self._pending_timer.stop()
        self._pending = None

    def _show_blocked(
        self, message: str, failure: ConnectResult | str, device_id: str, view: DeviceView | None
    ) -> None:
        """Thông báo thiết bị bị chặn (mục 10 A2), kèm nút hành động của mvp mục 8 khi có."""
        value = str(failure)
        level = InfoLevel.WARNING
        if value == ConnectResult.REJECTED.value and view is not None:
            repair = functools.partial(self._request_repair, device_id)
            self.view.show_notice(message, repair_action_text(), repair, level=level)
        elif value == ConnectResult.IDENTITY_MISMATCH.value:
            unpair = functools.partial(self._request_unpair, device_id)
            self.view.show_notice(message, unpair_action_text(), unpair, level=level)
        else:
            self.view.show_notice(message, level=level)

    def _on_connect_failed(self, view: DeviceView, result: ConnectResult) -> None:
        """`ConnectFailed` chặn khi còn lượt mới tới máy đó: gỡ thẻ, hủy lượt, hiện câu (bước 3)."""
        keys = [k for k in self._unoffered if k.device_id == view.device_id]
        if not keys:
            return
        message = send.blocked_message(result, self._peer(view.device_id, view))
        if message is None:
            return  # chưa kết nối được: lượt chờ tiếp, thẻ đã có câu chờ
        self._unoffered.difference_update(keys)
        host = self._host
        for key in keys:
            self.view.remove(key)
            self._sources.pop(key, None)
            if host is not None:
                self._dropped.add(key)
                self.cancel(key)
        self._show_blocked(message, result, view.device_id, view)

    def _request_repair(self, device_id: str) -> None:
        """Nút “Ghép đôi lại”: phát `repair_requested` với view mới nhất (luồng ghép đôi nối)."""
        view = self._window.devices.device(device_id)
        if view is not None:
            self.repair_requested.emit(view)

    def _request_unpair(self, device_id: str) -> None:
        """Nút “Hủy ghép đôi”: qua `conversations.unpair_requested` (luồng ghép đôi hỏi xác nhận)."""
        self._window.conversations.unpair_requested.emit(device_id)

    def _submit_send(self, device_id: str, paths: list[Path], peer: str) -> None:
        host = self._host
        if host is None:
            return
        token = self.view.add_pending(device_id, paths)  # “Đang chuẩn bị…” tới khi `send` trả về
        host.submit(
            lambda node: node.transfers.send(device_id, paths),
            functools.partial(self._on_send_done, host, peer, tuple(paths), token),
        )

    def _on_send_done(
        self, host: NodeHost, peer: str, paths: tuple[Path, ...], token: int, result: object
    ) -> None:
        self.view.remove_pending(token)
        if host is not self._host:
            return
        if isinstance(result, TransferKey):
            # Lượt đã có: nhớ đường dẫn cho “Gửi lại” (bỏ lượt đã gỡ vì bị chặn, không còn bong bóng,
            # hay đã xong DONE / UNPAIRED: lượt đó không bao giờ có nút “Gửi lại”).
            bubble = self.view.row(result)
            if result in self._dropped or bubble is None:
                return
            if bubble.result is not None and bubble.result.result in _NO_RESEND:
                return
            self._remember_sources(result, paths)
            self.view.set_resendable(result, True)
            return
        if not isinstance(result, BaseException):
            return
        message = send.send_error_message(result, peer)
        if message is not None:
            self.view.show_notice(message, level=InfoLevel.ERROR)

    def _remember_sources(self, key: TransferKey, paths: tuple[Path, ...]) -> None:
        """Nhớ đường dẫn của `key`; quá `SOURCES_MAX_TRANSFERS` lượt hay `SOURCES_MAX_PATHS` đường dẫn
        thì bỏ mục cũ nhất."""
        sources = self._sources
        sources.pop(key, None)
        sources[key] = paths
        total = sum(len(p) for p in sources.values())
        while len(sources) > 1 and (len(sources) > SOURCES_MAX_TRANSFERS or total > SOURCES_MAX_PATHS):
            old_key, old_paths = sources.popitem(last=False)
            total -= len(old_paths)
            self.view.set_resendable(old_key, False)

    def _on_history_needed(self, device_id: str) -> None:
        """Luồng của `device_id` cần lịch sử: đọc qua `call` khi node đã chạy, không thì chờ `started`."""
        host = self._host
        if host is None:
            return
        if self._started is not host:
            self._history_waiting = device_id
            return
        self._load_thread_history(host, device_id)

    def _load_thread_history(self, host: NodeHost, device_id: str) -> None:
        host.call(
            lambda node: node.transfers.history(device_id),
            functools.partial(self._on_thread_history, host, device_id),
        )

    def _on_thread_history(self, host: NodeHost, device_id: str, result: object) -> None:
        """Lịch sử với một máy cho luồng của nó; host cũ hay sau `close_all` thì bỏ."""
        if host is not self._host or self._closing:
            return
        if isinstance(result, BaseException):
            log.warning("không đọc được lịch sử với một máy: %s", type(result).__name__)
            self.view.history_failed(device_id)
            return
        entries = result if isinstance(result, tuple) else ()
        self.view.load_history(device_id, entries)

    def _on_snapshot(self, host: NodeHost, result: object) -> None:
        """Ảnh chụp lúc `started`: tên máy, trạng thái kết nối, các lượt chưa kết thúc."""
        if host is not self._host:
            return
        if isinstance(result, BaseException):
            log.warning("không đọc được danh sách lượt truyền: %s", type(result).__name__)
            return
        devices, infos = cast(tuple[tuple[DeviceView, ...], tuple[TransferInfo, ...]], result)
        for device in devices:
            if isinstance(device, DeviceView):
                self._names[device.device_id] = device.name
                self.view.set_connected(device.device_id, device.connected)
                self.offer_book.set_connected(device.device_id, device.connected)
        self.view.set_device_names(self._names)
        self.view.load_snapshot(infos)
        self._push_activity(sorted(self.activity.load_snapshot(infos, _wall_now())))

    def _on_history(self, host: NodeHost, result: object) -> None:
        """Lượt gần nhất của mỗi máy trong lịch sử (lúc `started`); chỉ dùng cho máy chưa có trong phiên."""
        if host is not self._host or self._closing:
            return
        if isinstance(result, BaseException):
            log.warning("không đọc được lịch sử lượt truyền: %s", type(result).__name__)
            return
        latest = cast(dict[str, FinishedTransfer], result)
        self._push_activity(sorted(self.activity.load_history(latest)))

    def _dialog_finished(self, dialog: QFileDialog) -> None:
        if self._dialog is dialog:
            self._dialog = None
        dialog.deleteLater()

    @staticmethod
    def _log_error(what: str, result: object) -> None:
        if isinstance(result, BaseException):
            log.warning("không %s được: %s", what, type(result).__name__)


def _update_key(update: TransferUpdate) -> TransferKey | None:
    """Khóa lượt của một `TransferUpdate` (có loại mang `key`, có loại mang `info.key`)."""
    key = getattr(update, "key", None)
    if isinstance(key, TransferKey):
        return key
    info = getattr(update, "info", None)
    return info.key if isinstance(info, TransferInfo) else None
