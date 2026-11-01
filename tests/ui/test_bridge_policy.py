"""Luật nhận yêu cầu ghép đôi của node GUI (spec D2 mục 1.3, 9.3, 9.8, 9.10; test 4.4 phần bridge).

Viết từ spec, không từ code:

- `Node` của `NodeHost` dựng với `accept_pairing=False` (đóng mặc định); ngay sau `start()` xong, trên
  thread node và **trước** khi post `started`, áp luật `accept_pairing = visible or đang chờ ghép đôi`.
- “Đang chờ ghép đôi” là một biến duy nhất của `NodeHost`, chỉ đọc/ghi trên thread node; đổi `visible`
  phải đi cùng một lần áp luật trong cùng một `host.call` nên trạng thái chờ ghép đôi không bị mất.
- `NodeHost.set_node_name(name)` (thread Qt) cập nhật tên đã nhớ (`node_name`).

Giả định tên chưa có trong spec (lấy theo code của giao-dien ở `ui/bridge.py`): phương thức
`NodeHost.apply_pairing_policy(node)` và `NodeHost.set_wait_pairing_open(node, flag)` (thread node);
hàm module `apply_pairing_policy(node, *, wait_pairing_open=False)` (spec 1.3).
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

integration = pytest.mark.integration


def host_call(qtbot, host, fn, timeout_ms: int = 5_000):
    box: dict = {}
    host.call(fn, lambda r: box.setdefault("r", r))
    qtbot.waitUntil(lambda: "r" in box, timeout=timeout_ms)
    if isinstance(box["r"], BaseException):
        raise box["r"]
    return box["r"]


@pytest.fixture
def probe(monkeypatch):
    """Ghi tham số `accept_pairing` lúc dựng `Node` và giá trị `node.accept_pairing` ngay lúc thread node
    post `_on_started` (tức trước khi `started` phát trên thread Qt)."""
    from teleport.ui import bridge

    seen = SimpleNamespace(ctor=[], at_started=[], threads=[])
    real_node = bridge.Node

    def make_node(*args, **kwargs):
        seen.ctor.append(kwargs.get("accept_pairing", "<không truyền>"))
        return real_node(*args, **kwargs)

    monkeypatch.setattr(bridge, "Node", make_node)
    real_post = bridge.NodeHost._post

    def post(self, fn):
        target = getattr(fn, "func", None)
        if getattr(target, "__name__", "") == "_on_started" and self._node is not None:
            seen.at_started.append(self._node.accept_pairing)
            seen.threads.append(threading.current_thread().name)
        return real_post(self, fn)

    monkeypatch.setattr(bridge.NodeHost, "_post", post)
    return seen


def start(qtbot, node_hosts, node_options, config):
    host = node_hosts(node_options(config))
    host.start()
    qtbot.waitUntil(lambda: host.running, timeout=15_000)
    return host


# =================================================================================================
# Hàm thuần
# =================================================================================================


@pytest.mark.parametrize(
    ("visible", "wait", "want"),
    [(True, False, True), (True, True, True), (False, True, True), (False, False, False)],
)
def test_apply_pairing_policy_truth_table(visible, wait, want):
    from teleport.ui.bridge import apply_pairing_policy

    node = SimpleNamespace(visible=visible, accept_pairing=not want)
    apply_pairing_policy(node, wait_pairing_open=wait)
    assert node.accept_pairing is want


def test_apply_pairing_policy_default_is_not_waiting():
    from teleport.ui.bridge import apply_pairing_policy

    node = SimpleNamespace(visible=False, accept_pairing=True)
    apply_pairing_policy(node)
    assert node.accept_pairing is False


# =================================================================================================
# NodeHost thật (127.0.0.1)
# =================================================================================================


@integration
def test_node_built_with_accept_pairing_false(qtbot, config_dir, node_options, node_hosts, probe):
    start(qtbot, node_hosts, node_options, config_dir("A"))
    assert probe.ctor == [False]


@integration
def test_policy_applied_before_started_visible_true(qtbot, config_dir, node_options, node_hosts, probe):
    host = start(qtbot, node_hosts, node_options, config_dir("A"))
    assert probe.at_started == [True], "máy hiện: luật đã áp trước khi post started"
    assert probe.threads and probe.threads[0] != threading.main_thread().name, "áp trên thread node"
    assert host_call(qtbot, host, lambda n: (n.visible, n.accept_pairing)) == (True, True)


@integration
def test_saved_hidden_start_accept_pairing_false(qtbot, config_dir, node_options, node_hosts, probe):
    d = config_dir("A")
    (d / "settings.json").write_text(json.dumps({"visible": False}), encoding="utf-8")
    host = start(qtbot, node_hosts, node_options, d)
    assert probe.ctor == [False]
    assert probe.at_started == [False]
    assert host_call(qtbot, host, lambda n: (n.visible, n.accept_pairing)) == (False, False)


@integration
def test_visible_change_and_policy_in_one_call(qtbot, config_dir, node_options, node_hosts):
    """Hợp đồng 1.3: `set_visible(v)` rồi áp luật trong cùng một `host.call` → accept_pairing == visible."""
    host = start(qtbot, node_hosts, node_options, config_dir("A"))

    def toggle(v):
        def run(node):
            node.set_visible(v)
            host.apply_pairing_policy(node)
            return node.visible, node.accept_pairing

        return run

    for v in (False, True, False, False, True):
        assert host_call(qtbot, host, toggle(v)) == (v, v)


@integration
def test_policy_keeps_wait_pairing_open_when_toggling_visible(qtbot, config_dir, node_options, node_hosts):
    """9.3: “đang chờ ghép đôi” do host giữ; đổi ẩn/hiện (cùng một call với áp luật) không làm mất nó."""
    host = start(qtbot, node_hosts, node_options, config_dir("A"))

    def toggle(v):
        def run(node):
            node.set_visible(v)
            host.apply_pairing_policy(node)
            return node.accept_pairing

        return run

    assert host_call(qtbot, host, toggle(False)) is False
    host_call(qtbot, host, lambda n: host.set_wait_pairing_open(n, True))
    assert host_call(qtbot, host, lambda n: n.accept_pairing) is True, "máy ẩn đang chờ ghép đôi: nhận"
    assert host_call(qtbot, host, toggle(True)) is True
    assert host_call(qtbot, host, toggle(False)) is True, "ẩn lại khi đang chờ ghép đôi: vẫn nhận"
    host_call(qtbot, host, lambda n: host.set_wait_pairing_open(n, False))
    assert host_call(qtbot, host, lambda n: n.accept_pairing) is False, "đóng chờ ghép đôi khi ẩn: thôi nhận"


def test_set_node_name_updates_cached_name(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    assert host.node_name is None
    host.set_node_name("May-Moi")
    assert host.node_name == "May-Moi"


@integration
def test_set_node_name_after_started_overrides_start_name(qtbot, config_dir, node_options, node_hosts):
    host = start(qtbot, node_hosts, node_options, config_dir("A"))
    old = host.node_name
    assert old
    host_call(qtbot, host, lambda n: n.set_name("Ten-Moi"))
    assert host.node_name == old, "host chỉ đổi tên đã nhớ khi bên gọi báo (9.10)"
    host.set_node_name("Ten-Moi")
    assert host.node_name == "Ten-Moi"
