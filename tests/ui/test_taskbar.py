"""Kiểm thử thanh tiến độ Windows Taskbar (`teleport.ui.taskbar`)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from teleport.ui.main_window import MainWindow
from teleport.ui.taskbar import TBPF_NOPROGRESS, TBPF_NORMAL, TBPF_PAUSED, TaskbarProgress


class _FakeBackend:
    def __init__(self) -> None:
        self.states: list[tuple[int, int]] = []
        self.values: list[tuple[int, int, int]] = []
        self.closed = False
        self.raise_on_update = False

    def set_progress_state(self, hwnd: int, flags: int) -> None:
        if self.raise_on_update:
            raise OSError("COM error")
        self.states.append((hwnd, flags))

    def set_progress_value(self, hwnd: int, completed: int, total: int) -> None:
        if self.raise_on_update:
            raise OSError("COM error")
        self.values.append((hwnd, completed, total))

    def close(self) -> None:
        self.closed = True


def test_taskbar_progress_states_and_error_isolation() -> None:
    backend = _FakeBackend()
    tb = TaskbarProgress(backend_factory=lambda: backend)

    assert tb.update(1234, 42, paused=False) is True
    assert backend.states[-1] == (1234, TBPF_NORMAL)
    assert backend.values[-1] == (1234, 42, 100)

    # Gọi trùng giá trị -> bỏ qua COM dư thừa
    assert tb.update(1234, 42, paused=False) is True
    assert len(backend.states) == 1

    assert tb.update(1234, 75, paused=True) is True
    assert backend.states[-1] == (1234, TBPF_PAUSED)
    assert backend.values[-1] == (1234, 75, 100)

    assert tb.update(1234, None) is True
    assert backend.states[-1] == (1234, TBPF_NOPROGRESS)

    # Lỗi COM -> bắt an toàn, đóng backend và tự vô hiệu hóa
    backend.raise_on_update = True
    assert tb.update(1234, 10) is False
    assert backend.closed is True
    assert tb.update(1234, 20) is False

    tb.dispose()


def test_main_window_set_progress_while_hidden_does_not_force_win_id(qtbot) -> None:
    w = MainWindow()
    qtbot.addWidget(w)
    w.set_progress(60)
    assert not w.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
