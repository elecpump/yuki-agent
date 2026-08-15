class FakeBus:
    """镜像 BusNode 语义：多 handler + 前缀匹配 + 同步 service map。

    publish 仅记录不派发（生产为异步）；测试手动触发 handler。
    """

    def __init__(self):
        self.published = []
        self.subscriptions: dict[str, list] = {}
        self.services = {}
        self._error_count = 0
        self.closed = False

    def subscribe(self, prefix, handler):
        self.subscriptions.setdefault(prefix, []).append(handler)

    def pause_subscriptions(self):
        pass

    def resume_subscriptions(self):
        pass

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def respond(self, service, handler):
        self.services[service] = handler

    def request(self, service, payload, timeout_ms=2000):
        handler = self.services.get(service)
        if handler is None:
            raise RuntimeError(f"service not found: {service}")
        return handler(payload)

    @property
    def error_count(self):
        return self._error_count

    def close(self):
        self.closed = True
