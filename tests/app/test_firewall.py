from teleport.app.firewall import (
    FirewallIssue,
    check_firewall_status,
    parse_netsh_currentprofile,
    parse_netsh_rule,
)


def test_parse_netsh_currentprofile_public_on():
    text = """
Public Profile Settings:
----------------------------------------------------------------------
State                                 ON
Firewall Policy                       BlockInbound,AllowOutbound
"""
    assert parse_netsh_currentprofile(text) is FirewallIssue.PUBLIC_NETWORK


def test_parse_netsh_currentprofile_public_off():
    text = """
Public Profile Settings:
----------------------------------------------------------------------
State                                 OFF
Firewall Policy                       BlockInbound,AllowOutbound
"""
    assert parse_netsh_currentprofile(text) is FirewallIssue.NONE


def test_parse_netsh_currentprofile_private():
    text = """
Private Profile Settings:
----------------------------------------------------------------------
State                                 ON
Firewall Policy                       BlockInbound,AllowOutbound
"""
    assert parse_netsh_currentprofile(text) is FirewallIssue.NONE


def test_parse_netsh_rule_block():
    text = """
Rule Name:                            Teleport Block
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private,Public
Action:                               Block
"""
    assert parse_netsh_rule(text) is True


def test_parse_netsh_rule_allow():
    text = """
Rule Name:                            Teleport (TCP-In)
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private
Action:                               Allow
"""
    assert parse_netsh_rule(text) is False


def test_check_firewall_status_public_network():
    def mock_runner(cmd: list[str]) -> str:
        if "currentprofile" in cmd:
            return "Public Profile Settings:\nState ON\n"
        return ""

    assert check_firewall_status(runner=mock_runner) is FirewallIssue.PUBLIC_NETWORK


def test_check_firewall_status_blocked():
    def mock_runner(cmd: list[str]) -> str:
        if "currentprofile" in cmd:
            return "Private Profile Settings:\nState ON\n"
        if "rule" in cmd:
            return "Rule Name: Teleport\nDirection: In\nAction: Block\n"
        return ""

    assert check_firewall_status(runner=mock_runner) is FirewallIssue.BLOCKED


def test_check_firewall_status_normal():
    def mock_runner(cmd: list[str]) -> str:
        if "currentprofile" in cmd:
            return "Private Profile Settings:\nState ON\n"
        if "rule" in cmd:
            return "Rule Name: Teleport (TCP-In)\nDirection: In\nAction: Allow\n"
        return ""

    assert check_firewall_status(runner=mock_runner) is FirewallIssue.NONE


def test_check_firewall_status_mixed_rules_does_not_false_alarm():
    # Khi máy có rule Allow của Teleport và rule Block của ứng dụng khác
    def mock_runner(cmd: list[str]) -> str:
        if "currentprofile" in cmd:
            return "Private Profile Settings:\nState ON\n"
        if "rule" in cmd:
            return """
Rule Name:                            Teleport (TCP-In)
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Action:                               Allow

Rule Name:                            Unrelated Block Rule
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Action:                               Block
"""
        return ""

    assert check_firewall_status(runner=mock_runner) is FirewallIssue.NONE


def test_check_firewall_status_disabled_block_rule_ignored():
    # Rule Block nhưng Enabled: No thì không chặn
    def mock_runner(cmd: list[str]) -> str:
        if "currentprofile" in cmd:
            return "Private Profile Settings:\nState ON\n"
        if "rule" in cmd:
            return """
Rule Name:                            Teleport Blocked Old
----------------------------------------------------------------------
Enabled:                              No
Direction:                            In
Action:                               Block
"""
        return ""

    assert check_firewall_status(runner=mock_runner) is FirewallIssue.NONE


def test_check_firewall_status_runner_exception():
    def broken_runner(cmd: list[str]) -> str:
        raise OSError("Lệnh không tồn tại")

    assert check_firewall_status(runner=broken_runner) is FirewallIssue.NONE
