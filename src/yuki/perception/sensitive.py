_DEFAULT_CLASS_BLACKLIST = frozenset({
    # 安全/系统
    "LogonUI",
    "Credential UI Host",
    "SecureDesktopHost",
    "LockAppHost",
    "Shell_TrayWnd",
    # 密码管理器（常见）
    "KeePassMainWindow",
    "BitwardenMainWindow",
    "1PasswordMainWindow",
})

_DEFAULT_TITLE_KEYWORDS = (
    "password", "passphrase", "credential", "secret",
    "密码", "口令", "凭据",
    "银行", "银行卡", "card", "wallet", "vault",
    "登录", "sign in", "login", "两因素", "2fa", "otp",
)

_DEFAULT_SECURE_DESKTOP_CLASSES = frozenset({
    "LogonUI",
    "SecureDesktopHost",
    "Credential UI Host",
})


class SensitiveDetector:
    """窗口级敏感检测：命中即应停止截屏并发布占位黑帧。

    纯逻辑：类名/标题关键词黑名单 + 安全桌面类。可注入自定义规则。
    """

    def __init__(
        self,
        class_blacklist: set[str] | None = None,
        title_keywords: tuple[str, ...] | None = None,
        secure_desktop_classes: set[str] | None = None,
    ) -> None:
        self._class_blacklist = (
            frozenset(class_blacklist) if class_blacklist is not None else _DEFAULT_CLASS_BLACKLIST
        )
        self._title_keywords = (
            title_keywords if title_keywords is not None else _DEFAULT_TITLE_KEYWORDS
        )
        self._secure_desktop_classes = (
            frozenset(secure_desktop_classes)
            if secure_desktop_classes is not None
            else _DEFAULT_SECURE_DESKTOP_CLASSES
        )

    def is_sensitive(self, class_name: str, title: str) -> bool:
        class_name = (class_name or "").strip()
        title = (title or "").strip()
        if class_name in self._secure_desktop_classes:
            return True
        if class_name in self._class_blacklist:
            return True
        title_lower = title.lower()
        return any(kw in title_lower for kw in self._title_keywords)
