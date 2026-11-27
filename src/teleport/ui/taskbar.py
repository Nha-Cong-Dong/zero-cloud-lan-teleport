"""Hiển thị tiến độ truyền tệp ngay trên biểu tượng thanh tác vụ Windows (`ITaskbarList3`).

Sử dụng trực tiếp giao diện COM `ITaskbarList3` qua `ctypes` khi chạy trên Windows có cửa sổ
thật; trên Linux/macOS, nền `offscreen`, khi cửa sổ chưa hiện hoặc khi môi trường không hỗ
trợ COM, mọi lời gọi đều trả về False an toàn không ném ngoại lệ.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

TBPF_NOPROGRESS = 0x0
TBPF_INDETERMINATE = 0x1
TBPF_NORMAL = 0x2
TBPF_ERROR = 0x4
TBPF_PAUSED = 0x8

_CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
_IID_ITASKBAR_LIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = 0x80010106


class TaskbarBackend(Protocol):
    """Giao diện tối thiểu để đặt tiến độ taskbar (dễ thay thế trong kiểm thử)."""

    def set_progress_state(self, hwnd: int, flags: int) -> None: ...

    def set_progress_value(self, hwnd: int, completed: int, total: int) -> None: ...

    def close(self) -> None: ...


class _WindowsTaskbarBackend:
    """Bọc `ITaskbarList3` qua vtable COM thuần trên Windows 64-bit/32-bit."""

    _owns_com: bool
    _ole32: Any
    _ptr: Any
    _release_fn: Any
    _hr_init_fn: Any
    _set_value_fn: Any
    _set_state_fn: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("chỉ có trên Windows")
        import ctypes
        from ctypes import POINTER, Structure, c_int, c_long, c_ulong, c_ulonglong, c_ushort, c_void_p

        class GUID(Structure):
            _fields_ = [
                ("Data1", c_ulong),
                ("Data2", c_ushort),
                ("Data3", c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        ole32 = ctypes.WinDLL("ole32")
        ole32.IIDFromString.argtypes = [ctypes.c_wchar_p, POINTER(GUID)]
        ole32.IIDFromString.restype = c_long
        ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
        ole32.CoInitializeEx.restype = c_long
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, c_ulong, POINTER(GUID), POINTER(c_void_p)]
        ole32.CoCreateInstance.restype = c_long

        hr_init_com = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        self._owns_com = hr_init_com in (_S_OK, _S_FALSE)
        if not self._owns_com and (hr_init_com & 0xFFFFFFFF) != _RPC_E_CHANGED_MODE:
            raise OSError(f"CoInitializeEx thất bại: 0x{hr_init_com & 0xFFFFFFFF:08x}")

        clsid = GUID()
        iid = GUID()
        if ole32.IIDFromString(_CLSID_TASKBAR_LIST, ctypes.byref(clsid)) < 0:
            self._uninit(ole32)
            raise OSError("CLSID không hợp lệ")
        if ole32.IIDFromString(_IID_ITASKBAR_LIST3, ctypes.byref(iid)) < 0:
            self._uninit(ole32)
            raise OSError("IID không hợp lệ")

        ptr = c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(ptr)
        )
        if hr < 0 or not ptr.value:
            self._uninit(ole32)
            raise OSError(f"CoCreateInstance thất bại: 0x{hr & 0xFFFFFFFF:08x}")

        self._ole32 = ole32
        self._ptr: Any = ptr
        vtbl = ctypes.cast(ctypes.cast(ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
        winfn = ctypes.WINFUNCTYPE
        release_t = winfn(c_ulong, c_void_p)
        hr_init_t = winfn(c_long, c_void_p)
        set_val_t = winfn(c_long, c_void_p, c_void_p, c_ulonglong, c_ulonglong)
        set_state_t = winfn(c_long, c_void_p, c_void_p, c_int)

        self._release_fn = release_t(vtbl[2])
        self._hr_init_fn = hr_init_t(vtbl[3])
        self._set_value_fn = set_val_t(vtbl[9])
        self._set_state_fn = set_state_t(vtbl[10])

        hr2 = self._hr_init_fn(self._ptr)
        if hr2 < 0:
            self.close()
            raise OSError(f"ITaskbarList3.HrInit thất bại: 0x{hr2 & 0xFFFFFFFF:08x}")

    def _uninit(self, ole32: Any) -> None:
        if self._owns_com:
            self._owns_com = False
            with contextlib.suppress(Exception):
                ole32.CoUninitialize()

    def set_progress_state(self, hwnd: int, flags: int) -> None:
        if self._ptr is not None:
            hr = self._set_state_fn(self._ptr, hwnd, int(flags))
            if hr < 0:
                raise OSError(f"SetProgressState thất bại: 0x{hr & 0xFFFFFFFF:08x}")

    def set_progress_value(self, hwnd: int, completed: int, total: int) -> None:
        if self._ptr is not None:
            hr = self._set_value_fn(self._ptr, hwnd, int(completed), int(total))
            if hr < 0:
                raise OSError(f"SetProgressValue thất bại: 0x{hr & 0xFFFFFFFF:08x}")

    def close(self) -> None:
        ptr, self._ptr = self._ptr, None
        if ptr is not None:
            with contextlib.suppress(Exception):
                self._release_fn(ptr)
        self._uninit(self._ole32)


def _is_offscreen() -> bool:
    try:
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication.platformName().lower() in ("offscreen", "minimal")
    except Exception:
        return False


class TaskbarProgress:
    """Điều khiển thanh tiến độ trên nút taskbar Windows cho một cửa sổ."""

    def __init__(self, backend_factory: Callable[[], TaskbarBackend | None] | None = None) -> None:
        self._backend_factory = backend_factory
        self._backend: TaskbarBackend | None = None
        self._disabled = False
        self._last_key: tuple[int, int, int] | None = None

    def update(self, hwnd: int | None, percent: int | None, *, paused: bool = False) -> bool:
        """Cập nhật % tổng (`0..100`) hoặc xóa tiến độ (`None`) trên taskbar; trả True nếu áp xong."""
        if self._disabled or hwnd is None or hwnd <= 0:
            return False
        if self._backend_factory is None and (sys.platform != "win32" or _is_offscreen()):
            return False
        if percent is None:
            state = TBPF_NOPROGRESS
            value = 0
        else:
            value = min(100, max(0, int(percent)))
            state = TBPF_PAUSED if paused else TBPF_NORMAL
        key = (int(hwnd), state, value)
        if key == self._last_key:
            return True
        backend = self._ensure_backend()
        if backend is None:
            return False
        try:
            backend.set_progress_state(int(hwnd), state)
            if state != TBPF_NOPROGRESS:
                backend.set_progress_value(int(hwnd), value, 100)
        except Exception as e:
            log.debug("không cập nhật được tiến độ taskbar: %s", type(e).__name__)
            self._disable()
            return False
        self._last_key = key
        return True

    def _ensure_backend(self) -> TaskbarBackend | None:
        if self._backend is not None:
            return self._backend
        try:
            if self._backend_factory is not None:
                self._backend = self._backend_factory()
            else:
                self._backend = _WindowsTaskbarBackend()
        except Exception as e:
            log.debug("không khởi tạo được taskbar progress: %s", type(e).__name__)
            self._disabled = True
            return None
        if self._backend is None:
            self._disabled = True
        return self._backend

    def _disable(self) -> None:
        self._disabled = True
        self._last_key = None
        backend, self._backend = self._backend, None
        if backend is not None:
            with contextlib.suppress(Exception):
                backend.close()

    def dispose(self) -> None:
        """Giải phóng tài nguyên COM nếu đã khởi tạo."""
        self._last_key = None
        backend, self._backend = self._backend, None
        if backend is not None:
            with contextlib.suppress(Exception):
                backend.close()
