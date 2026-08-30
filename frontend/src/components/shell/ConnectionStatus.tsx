import { useEffect, useState } from "react";
import { STATUS_STALE_AFTER_MS } from "../../config/runtime";
import { useAppStore } from "../../state/store";

type DisplayState = { className: "ok" | "bad" | "stale"; label: string };

export function ConnectionStatus() {
  const [now, setNow] = useState(Date.now());
  const statusWsState = useAppStore((state) => state.statusWsState);
  const chatWsState = useAppStore((state) => state.chatWsState);
  const statusLastMessageAt = useAppStore((state) => state.statusLastMessageAt);
  const restReachable = useAppStore((state) => state.restReachable);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5_000);
    return () => clearInterval(timer);
  }, []);

  const statusStale =
    statusLastMessageAt !== null && now - statusLastMessageAt > STATUS_STALE_AFTER_MS;
  let display: DisplayState;
  if (restReachable === false && statusWsState !== "open" && chatWsState !== "open") {
    display = { className: "bad", label: "后端不可达" };
  } else if (chatWsState !== "open" && restReachable === true) {
    display = { className: "stale", label: "对话重连中" };
  } else if ((statusWsState !== "open" || statusStale) && restReachable === true) {
    display = { className: "stale", label: "状态流异常" };
  } else if (statusWsState === "open" && chatWsState === "open" && restReachable === true) {
    display = { className: "ok", label: "服务正常" };
  } else {
    display = { className: "stale", label: "正在连接" };
  }

  return (
    <div className="connection-line">
      <span><i className={`status-dot ${display.className}`} />{display.label}</span>
      <span>REST {restReachable === true ? "✓" : restReachable === false ? "×" : "…"}</span>
    </div>
  );
}
