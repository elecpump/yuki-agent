import pytest

from yuki.cognition.sensitive import SensitiveFilter


def test_detects_id_card():
    f = SensitiveFilter()
    assert f.is_sensitive("身份证号是110101199003074518")


def test_detects_bank_card():
    f = SensitiveFilter()
    assert "bank_card" in f.scan("卡号 6222021234567890123")


def test_detects_phone():
    f = SensitiveFilter()
    assert f.is_sensitive("联系电话 13800138000")


def test_detects_password_keyword():
    f = SensitiveFilter()
    assert f.is_sensitive("密码：abc123 请勿泄露")


def test_allows_normal_text():
    f = SensitiveFilter()
    assert f.scan("这篇文章讨论了气候变化的影响。") == []
    assert f.is_sensitive("如何写代码 - 知乎") is False


def test_custom_patterns_override():
    f = SensitiveFilter(patterns=(r"\bSECRET\d+\b",))
    assert f.is_sensitive("SECRET42 是机密") is True
    assert f.is_sensitive("普通内容") is False
