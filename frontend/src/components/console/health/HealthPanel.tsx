import { Card, Tag } from "antd";
import { useAppStore } from "../../../state/store";
import { ProcessCard } from "./ProcessCard";

export function HealthPanel() {
  const processes = useAppStore((state) => state.processes);
  const hub = useAppStore((state) => state.hub);
  const gateway = useAppStore((state) => state.gateway);

  return (
    <div className="health-stack">
      <Card size="small">
        <div className="health-card-title">
          <span><i className={`status-dot ${gateway?.healthy ? "ok" : "bad"}`} />Gateway</span>
          <Tag bordered={false}>{gateway?.healthy ? "正常" : "不可达"}</Tag>
        </div>
        <div className="component-detail">
          {gateway ? `started: ${String(gateway.started)}` : "等待健康快照"}
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
