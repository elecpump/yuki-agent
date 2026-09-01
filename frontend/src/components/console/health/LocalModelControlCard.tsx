import { Card, Spin, Switch, Tag } from "antd";
import { useLocalModelControl } from "../../../state/hooks/useLocalModelControl";
import type { LocalModelState } from "../../../types/api";

const STATE_LABELS: Record<LocalModelState, string> = {
  unavailable: "配置禁用",
  disabled: "已关闭",
  enabling: "开启中",
  enabled: "已开启",
  disabling: "关闭中",
  recovering: "恢复中",
  failed: "失败",
};

const STATE_COLORS: Partial<Record<LocalModelState, string>> = {
  enabled: "success",
  enabling: "processing",
  disabling: "processing",
  recovering: "warning",
  failed: "error",
};

function statusDotState(state: LocalModelState | undefined): "ok" | "bad" | "stale" {
  if (state === "enabled") return "ok";
  if (state === "failed") return "bad";
  return "stale";
}

export function LocalModelControlCard() {
  const { status, loading, error, setEnabled } = useLocalModelControl();
  const state = status?.state;
  const unavailable = !status || !status.available;
  const busy = loading || state === "enabling" || state === "disabling" || state === "recovering";
  const displayError = error || status?.last_error;
  const dotState = statusDotState(state);

  return (
    <Card size="small">
      <div className="health-card-title">
        <span><i className={`status-dot ${dotState}`} />本地对话模型</span>
        <span className="local-model-actions">
          <Tag bordered={false} color={state ? STATE_COLORS[state] : undefined}>
            {state ? STATE_LABELS[state] : "读取中"}
          </Tag>
          <Switch
            aria-label="本地对话模型"
            checked={status?.enabled ?? false}
            disabled={busy || unavailable}
            onChange={(enabled) => void setEnabled(enabled)}
          />
        </span>
      </div>
      {busy && (
        <div className="component-detail local-model-progress">
          <Spin size="small" /> 正在排队或切换
        </div>
      )}
      {status && (
        <div className="component-detail">
          模型驻留：{status.loaded ? "是" : "否"} · 活跃调用：{status.active_calls}
        </div>
      )}
      {displayError && <div className="component-detail local-model-error">{displayError}</div>}
      <div className="component-detail">运行时设置；Yuki 重启后恢复配置值</div>
    </Card>
  );
}
