import type { ComponentHealth } from "../../../types/api";

function formatDetailValue(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return "[无法序列化]";
    }
  }
  return String(value);
}

export function ComponentList({ components }: { components: Record<string, ComponentHealth> }) {
  const entries = Object.entries(components);
  if (!entries.length) return <div className="component-detail">暂无组件数据</div>;
  return (
    <div className="component-list">
      {entries.map(([name, health]) => (
        <div className="component-item" key={name}>
          <div className="component-row">
            <span>{name}</span>
            <span>{health.ok ? "正常" : "异常"}</span>
          </div>
          {Object.keys(health.detail || {}).length > 0 && (
            <div className="component-detail">
              {Object.entries(health.detail)
                .map(([key, value]) => `${key}: ${formatDetailValue(value)}`)
                .join(" · ")}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
