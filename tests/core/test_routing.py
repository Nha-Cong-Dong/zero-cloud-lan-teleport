"""Kiểm thử sans-IO cho quy tắc tự phân loại thư mục nhận (`teleport.core.routing`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from teleport.core.routing import (
    MAX_PATTERNS_PER_RULE,
    MAX_ROUTING_RULES,
    RoutingRule,
    match_routing_rule,
    parse_patterns,
    routing_rule_from_dict,
    routing_rule_to_dict,
)

DEV_A = "a" * 32
DEV_B = "b" * 32


def test_parse_patterns_normalizes_separators_and_dedupes() -> None:
    assert parse_patterns("  *.JPG, *.png;  *.jpg  report-*.pdf ") == (
        "*.JPG",
        "*.png",
        "report-*.pdf",
    )
    assert parse_patterns("   ") == ()


def test_routing_rule_validation(tmp_path: Path) -> None:
    dest = str(tmp_path / "out")
    rule = RoutingRule(id="r-1", name="Video", destination=dest, device_id=DEV_A, patterns=("*.mp4",))
    roundtrip = routing_rule_from_dict(routing_rule_to_dict(rule))
    assert roundtrip == rule

    with pytest.raises(ValueError):
        RoutingRule(id="bad id!", name="X", destination=dest)
    with pytest.raises(ValueError):
        RoutingRule(id="r1", name="X", destination="relative/path")
    with pytest.raises(ValueError):
        RoutingRule(id="r1", name="X", destination=dest, patterns=("sub/*.png",))
    with pytest.raises(ValueError):
        RoutingRule(
            id="r1",
            name="X",
            destination=dest,
            patterns=tuple(f"*.e{i}" for i in range(MAX_PATTERNS_PER_RULE + 1)),
        )


def test_match_routing_rule_order_and_device_filter(tmp_path: Path) -> None:
    dest_vid = str(tmp_path / "Videos")
    dest_dev = str(tmp_path / "FromB")
    rules = (
        RoutingRule(id="disabled", name="Off", destination=dest_vid, patterns=("*.mp4",), enabled=False),
        RoutingRule(
            id="from-b",
            name="From B Docs",
            destination=dest_dev,
            device_id=DEV_B,
            patterns=("*.pdf",),
        ),
        RoutingRule(id="any-pdf", name="All PDFs", destination=dest_vid, patterns=("*.pdf",)),
        RoutingRule(id="catchall", name="All from A", destination=dest_vid, device_id=DEV_A),
    )

    assert match_routing_rule(rules, device_id=DEV_B, filename="a.mp4") is None
    assert match_routing_rule(rules, device_id=DEV_B, filename="doc.PDF").id == "from-b"
    assert (
        match_routing_rule(rules, device_id=DEV_A, filename="doc.pdf", rel_path="sub/doc.pdf").id == "any-pdf"
    )
    assert match_routing_rule(rules, device_id=DEV_A, filename="notes.txt").id == "catchall"
    assert MAX_ROUTING_RULES == 64
