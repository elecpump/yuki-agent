import { Tabs } from "antd";
import { useAppStore } from "../../state/store";
import type { ConsoleTab } from "../../state/slices/uiSlice";
import { HealthPanel } from "./health/HealthPanel";
import { Placeholder } from "./Placeholder";

const items = [
  { key: "health", label: "健康", children: <HealthPanel /> },
  { key: "memory", label: "记忆", children: <Placeholder title="记忆管理" /> },
  { key: "soul", label: "Soul", children: <Placeholder title="Soul" /> },
  { key: "config", label: "配置", children: <Placeholder title="配置" /> },
  { key: "perception", label: "感知", children: <Placeholder title="感知" /> },
];

export function ConsoleTabs() {
  const activeTab = useAppStore((state) => state.activeTab);
  const setTab = useAppStore((state) => state.setTab);
  return (
    <Tabs
      size="small"
      activeKey={activeTab}
      onChange={(key) => setTab(key as ConsoleTab)}
      items={items}
    />
  );
}
