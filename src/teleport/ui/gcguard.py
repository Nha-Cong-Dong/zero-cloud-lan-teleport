"""Bộ thu gom rác vòng tham chiếu chỉ chạy trên thread Qt (`GcGuard`).

**Vì sao cần.** Bộ thu gom vòng tham chiếu (cyclic GC) của CPython tự chạy ở *bất kỳ* thread nào
đang cấp phát đối tượng khi bộ đếm vượt ngưỡng. Tiến trình giao diện có thread node, thread đĩa
(`DiskExecutor`) và các thread khác cùng cấp phát; nếu một đối tượng PySide (widget, `QTimer`, hộp
hỏi nhận file đã đóng…) nằm trong một vòng tham chiếu, GC chạy ở thread node sẽ hủy nó (gọi
destructor C++) **ngoài thread Qt**, trong lúc thread Qt có thể đang dùng cùng đối tượng hay cùng
cấu trúc của Qt. Kết quả là lỗi bộ nhớ, crash ngẫu nhiên (đã thấy trên CI macOS: segfault khi thread
`teleport-node` đang “Garbage-collecting”). Qt yêu cầu đối tượng chỉ bị hủy trên thread sở hữu nó.

**Cách làm** (mẫu “GarbageCollector” quen thuộc của ứng dụng PyQt/PySide): `start()` tắt GC tự
động (`gc.disable()`) và bật một `QTimer` trên thread Qt; mỗi `INTERVAL_MS` hẹn giờ so
`gc.get_count()` với `gc.get_threshold()` và gọi `gc.collect(gen)` cho thế hệ già nhất đã vượt
ngưỡng, như GC tự động làm, nhưng luôn trên thread Qt. Đếm cấp phát của CPython vẫn chạy khi GC tắt
nên ngưỡng vẫn có nghĩa. Đối tượng không nằm trong vòng tham chiếu vẫn được giải phóng ngay bằng
đếm tham chiếu ở thread thả nó, như trước.

**Chu kỳ `INTERVAL_MS` = 500 ms.** Mỗi nhịp không vượt ngưỡng chỉ đọc hai bộ ba số, gần như không
tốn gì. Chu kỳ ngắn giữ thế hệ trẻ nhỏ (chi phí một lần `collect(0)` tỉ lệ với số đối tượng trẻ),
nên lúc truyền file nhanh mỗi lần thu gom vẫn chỉ vài ms và không làm giật giao diện. Cấp phát dồn
vượt ngưỡng nhiều lần trong một nhịp thì nhịp đó thu tiếp tới khi dưới ngưỡng, tối đa
`MAX_COLLECTS_PER_TICK` lần, để rác vòng không dồn nhanh hơn tốc độ thu; chu kỳ dài hơn (mẫu gốc
dùng vài giây) để dồn nhiều rác hơn mỗi lần.

**Python 3.12 và 3.14.** Vài bản 3.14 dùng GC tăng dần (thế hệ trẻ + “increment” của thế hệ già,
ngưỡng thế hệ 2 là 0, `collect(1)` chỉ chạy một phần), các bản 3.14 sau quay lại GC theo thế hệ;
`get_count()` / `get_threshold()` / `collect(gen)` có ở cả hai. Ngưỡng bằng 0 nghĩa là “không leo
lên thế hệ đó” (ngưỡng thế hệ 0 bằng 0 nghĩa là người dùng đã tắt GC theo ngưỡng: không thu gom theo
đếm). Nếu hai hàm trên không trả bộ ba số như mong đợi (bản Python sau đổi API), mỗi nhịp thu gom
thế hệ 0 và cứ `FALLBACK_FULL_EVERY` nhịp thì thu gom toàn bộ.

**Lồng nhau.** `start()` nhớ `gc.isenabled()` lúc đó; `stop()` dừng hẹn giờ, thu gom một lần trên
thread Qt rồi khôi phục đúng trạng thái đã nhớ. Nhiều guard lồng nhau (fixture test bọc ngoài,
`ui.app.main` bọc trong) nên dừng theo thứ tự ngược lúc bật.

**Giới hạn.** Hẹn giờ chỉ chạy khi vòng sự kiện Qt chạy; lúc thread Qt bị chặn thì rác vòng chỉ
dồn lại, không crash. Sau `stop()` GC tự động bật lại (nếu trước đó bật): nếu thread node vẫn còn
sống (ví dụ `Node.close()` treo quá hạn `join` trong `main`), GC có thể lại chạy ở thread đó với
rác mới tạo sau lần thu gom cuối; lần thu gom trong `stop()` đã dọn mọi rác Qt có tới lúc đó, và
ngay sau đó `main` thoát tiến trình (`os._exit`), nên chấp nhận được. Thư viện gọi thẳng
`gc.collect()` ở thread khác thì guard không chặn được (mã của Teleport không làm vậy).
"""

from __future__ import annotations

import gc

from PySide6.QtCore import QTimer

INTERVAL_MS = 500
MAX_COLLECTS_PER_TICK = 4  # cấp phát dồn trong một nhịp: thu tiếp tới khi dưới ngưỡng, có giới hạn
FALLBACK_FULL_EVERY = 20  # nhịp; chỉ dùng khi không đọc được ngưỡng/bộ đếm (≈ 10 s)


def _triple(value: object) -> tuple[int, int, int] | None:
    """Bộ ba số nguyên đầu của `gc.get_count()` / `gc.get_threshold()`; khác dạng thì `None`."""
    if not isinstance(value, tuple) or len(value) < 3:
        return None
    a, b, c = value[:3]
    if not all(isinstance(x, int) for x in (a, b, c)):
        return None
    return a, b, c


def generation_to_collect(count: tuple[int, int, int], threshold: tuple[int, int, int]) -> int | None:
    """Thế hệ cần thu gom theo bộ đếm và ngưỡng (như GC tự động), hay `None` nếu chưa tới ngưỡng."""
    if threshold[0] <= 0 or count[0] <= threshold[0]:
        return None
    gen = 0
    if threshold[1] > 0 and count[1] > threshold[1]:
        gen = 1
        if threshold[2] > 0 and count[2] > threshold[2]:
            gen = 2
    return gen


class GcGuard:
    """Tắt GC tự động và thu gom định kỳ trên thread Qt. Dựng, `start`, `stop` trên thread Qt."""

    def __init__(self, interval_ms: int = INTERVAL_MS) -> None:
        self._interval_ms = interval_ms
        self._timer: QTimer | None = None
        self._was_enabled: bool | None = None
        self._ticks = 0
        self.collections = 0  # số lần hẹn giờ đã gọi `gc.collect` (cho test và chẩn đoán)

    @property
    def active(self) -> bool:
        return self._was_enabled is not None

    def start(self) -> None:
        """Nhớ trạng thái GC, tắt GC tự động, bật hẹn giờ. Gọi lần hai khi đang bật thì không làm gì."""
        if self.active:
            return
        self._was_enabled = gc.isenabled()
        gc.disable()
        timer = QTimer()
        timer.setInterval(self._interval_ms)
        timer.timeout.connect(self._tick)
        timer.start()
        self._timer = timer

    def stop(self) -> None:
        """Dừng hẹn giờ, thu gom toàn bộ một lần trên thread này, khôi phục trạng thái GC đã nhớ."""
        if not self.active:
            return
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.stop()
            timer.timeout.disconnect(self._tick)
            del timer  # không có cha: thả tham chiếu cuối là hủy ngay, trên thread này
        was_enabled, self._was_enabled = self._was_enabled, None
        gc.collect()
        if was_enabled:
            gc.enable()

    def _tick(self) -> None:
        # Hẹn giờ được dựng trên thread Qt nên slot này luôn chạy trên thread Qt.
        self._ticks += 1
        for _ in range(MAX_COLLECTS_PER_TICK):
            try:
                count = _triple(gc.get_count())
                threshold = _triple(gc.get_threshold())
            except Exception:  # pragma: no cover - API GC đổi ở bản Python sau
                count = threshold = None
            if count is None or threshold is None:  # pragma: no cover - như trên
                gc.collect(2 if self._ticks % FALLBACK_FULL_EVERY == 0 else 0)
                self.collections += 1
                return
            gen = generation_to_collect(count, threshold)
            if gen is None:
                return
            gc.collect(gen)
            self.collections += 1
