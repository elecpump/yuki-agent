import type { ReactNode } from "react";
import type { ComponentHealth } from "../../../types/api";

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function DetailValue({ value }: { value: unknown }): ReactNode {
  if (value === null) return <span className="kv-dim">null</span>;
  if (typeof value === "boolean") return <span>{String(value)}</span>;
  if (typeof value === "number") return <span>{String(value)}</span>;
  if (typeof value === "string") {
    return value === "" ? <span className="kv-dim">""</span> : <span>{value}</span>;
  }
  if (Array.isArray(value)) {
    if (!value.length) return <span className="kv-dim">[]</span>;
    return (
      <span>
        {value.map((item, index) => (
          <span key={index} className="kv-list-item">
            {isPlainObject(item) ? <DetailRows data={item} /> : <DetailValue value={item} />}
          </span>
        ))}
      </span>
    );
  }
  return <span>{String(value)}</span>;
}

function DetailRows({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  if (!entries.length) return <span className="kv-dim">{"{}"}</span>;
  return (
    <>
      {entries.map(([key, value]) => {
        if (isPlainObject(value)) {
          const sub = Object.entries(value);
          if (!sub.length) {
            return (
              <div className="kv-row" key={key}>
                <span className="kv-key">{key}</span>
                <span className="kv-dim">{"{}"}</span>
              </div>
            );
          }
          return (
            <div className="kv-group" key={key}>
              <div className="kv-group-title">{key}</div>
              <div className="kv-group-body">
                <DetailRows data={value} />
              </div>
            </div>
          );
        }
        return (
          <div className="kv-row" key={key}>
            <span className="kv-key">{key}</span>
            <span className="kv-value">
              <DetailValue value={value} />
            </span>
          </div>
        );
      })}
    </>
  );
}

export function ComponentList({ components }: { components: Record<string, ComponentHealth> }) {
  const entries = Object.entries(components);
  if (!entries.length) return <div className="component-detail">暂无组件数据</div>;
  return (
    <div className="component-list">
      {entries.map(([name, health]) => {
        const detail = health.detail || {};
        const hasDetail = Object.keys(detail).length > 0;
        return (
          <div className="component-item" key={name}>
            <div className="component-row">
              <span>{name}</span>
              <span className={health.ok ? "component-ok" : "component-bad"}>
                {health.ok ? "正常" : "异常"}
              </span>
            </div>
            {hasDetail && (
              <div className="component-detail">
                <DetailRows data={detail} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
