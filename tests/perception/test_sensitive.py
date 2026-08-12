import pytest

from yuki.perception.sensitive import SensitiveDetector


def test_default_detector_marks_bank_in_title():
    det = SensitiveDetector()
    assert det.is_sensitive(class_name="Chrome_WidgetWin_1", title="网上银行登录 - ABC银行") is True


def test_default_detector_marks_password_keyword():
    det = SensitiveDetector()
    assert det.is_sensitive(class_name="Anything", title="Enter your password") is True


def test_default_detector_marks_secure_desktop_class():
    det = SensitiveDetector()
    assert det.is_sensitive(class_name="LogonUI", title="") is True


def test_default_detector_allows_normal_window():
    det = SensitiveDetector()
    assert det.is_sensitive(class_name="Chrome_WidgetWin_1", title="如何写代码 - 知乎") is False


def test_custom_blacklist_overrides():
    det = SensitiveDetector(
        class_blacklist={"SecretClass"},
        title_keywords=("机密",),
        secure_desktop_classes=set(),
    )
    assert det.is_sensitive(class_name="SecretClass", title="普通") is True
    assert det.is_sensitive(class_name="Normal", title="这是机密文档") is True
    assert det.is_sensitive(class_name="Normal", title="普通标题") is False
    # 默认银行关键词不再命中（自定义覆盖默认）
    assert det.is_sensitive(class_name="Chrome_WidgetWin_1", title="网上银行") is False
