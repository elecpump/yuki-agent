import { Card, Collapse, Tag } from "antd";
import { useAppStore } from "../../../state/store";
import type { ComponentHealth, HubHealth, JsonRecord } from "../../../types/api";
import { ComponentList } from "./ComponentList";
import { ProcessCard } from "./ProcessCard";

function hubComponents(hub: HubHealth | null): Record<string, ComponentHealth> {
  return Object.fromEntries(
    Object.entries(hub?.components || {}).map(([name, raw]) => {
      const detail = Object.fromEntries(Object.entries(raw).filter(([key]) => key !== "ok"));
      return [name, { ok: raw.ok !== false, detail: detail as JsonRecord }];
    }),
  );
}

export function HealthPanel() {
  const processes = useAppStore((state) => state.processes);
  const hub = useAppStore((state) => state.hub);
  const gateway = useAppStore((state) => state.gateway);
  const normalizedHubComponents = hubComponents(hub);
  const gatewayStatus = gateway ? (gateway.healthy ? "ok" : "bad") : "stale";
  const gatewayLabel = gateway ? (gateway.healthy ? "正常" : "不可达") : "未知";
  const gatewayFacts = gateway
    ? [
        `started: ${String(gateway.started)}`,
        `更新时间: ${new Date(gateway.ts * 1000).toLocaleTimeString()}`,
      ]
    : [];
  const hubFacts = hub
    ? [
        hub.pid == null ? null : `PID ${hub.pid}`,
        hub.uptime_s == null ? null : `运行 ${hub.uptime_s.toFixed(0)}s`,
        hub.error_count == null ? null : `错误 ${hub.error_count}`,
      ].filter(Boolean)
    : [];

  return (
    <div className="health-stack">
      <Card size="small">
        <div className="health-card-title">
          <span><i className={`status-dot ${gatewayStatus}`} />Gateway</span>
          <Tag bordered={false}>{gatewayLabel}</Tag>
        </div>
        <div className="component-detail">
          {gateway ? gatewayFacts.join(" · ") : "等待健康快照"}
        </div>
      </Card>
      <Card size="small">
        <div className="health-card-title">
          <span>
            <i className={`status-dot ${hub?.healthy === true ? "ok" : hub?.healthy === false ? "bad" : "stale"}`} />
            Bus Hub
          </span>
          <Tag bordered={false}>
            {hub?.healthy === true ? "正常" : hub?.healthy === false ? "不可达" : "未知"}
          </Tag>
        </div>
        {hub?.error && <div className="component-detail">{hub.error}</div>}
        {hubFacts.length > 0 && <div className="component-detail">{hubFacts.join(" · ")}</div>}
        {Object.keys(normalizedHubComponents).length > 0 && (
          <Collapse
            ghost
            size="small"
            items={[{
              key: "components",
              label: `${Object.keys(normalizedHubComponents).length} 个 Hub 组件`,
              children: <ComponentList components={normalizedHubComponents} />,
            }]}
          />
        )}
      </Card>
      {Object.entries(processes).map(([name, process]) => (
        <ProcessCard name={name} process={process} key={name} />
      ))}
      {Object.keys(processes).length === 0 && (
        <Card size="small"><div className="component-detail">等待进程心跳…</div></Card>
      )}
    </div>
  );
}
