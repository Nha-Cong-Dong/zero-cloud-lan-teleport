"""`validate_file_name` dùng regex biên dịch sẵn thay cho `unicodedata.category`: so trên mọi code point."""

import sys
import unicodedata

from teleport.core.messages import MessageError, validate_file_name


def test_forbidden_set_is_exactly_cc_and_cs_for_every_code_point():
    mismatches = []
    for cp in range(sys.maxunicode + 1):
        ch = chr(cp)
        expected = unicodedata.category(ch) in ("Cc", "Cs")
        try:
            validate_file_name(f"a{ch}b")
            rejected = False
        except MessageError:
            rejected = True
        if rejected != expected:
            mismatches.append(f"U+{cp:04X}")
    assert not mismatches, mismatches[:20]
