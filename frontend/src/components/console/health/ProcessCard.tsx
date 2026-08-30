import { Card, Collapse, Tag } from "antd";
import type { ProcessHealth } from "../../../types/api";
import { ComponentList } from "./ComponentList";

export function ProcessCard({ name, process }: { name: string; process: ProcessHealth }) {
  const status = !process.fresh ? "stale" : process.healthy ? "ok" : "bad";
  const label = !process.fresh ? "心跳过期" : process.healthy ? "正常" : "异常";
  const facts = [
    process.last_seen_age_s == null ? null : `心跳 ${process.last_seen_age_s.toFixed(1)}s 前`,
    process.pid == null ? null : `PID ${process.pid}`,
    process.uptime_s == null ? null : `运行 ${process.uptime_s.toFixed(0)}s`,
    process.error_count == null ? null : `错误 ${process.error_count}`,
  ].filter(Boolean);

  return (
    <Card size="small">
      <div className="health-card-title">
        <span><i className={`status-dot ${status}`} />{name}</span>
        <Tag bordered={false}>{label}</Tag>
      </div>
      {facts.length > 0 && <div className="component-detail">{facts.join(" · ")}</div>}
      <Collapse
        ghost
        size="small"
        items={[{
          key: "components",
          label: `${Object.keys(process.components || {}).length} 个组件`,
          children: <ComponentList components={process.components || {}} />,
        }]}
      />
    </Card>
  );
}
