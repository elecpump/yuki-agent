import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { HealthPanel } from "../src/components/console/health/HealthPanel";
import { useAppStore } from "../src/state/store";

describe("HealthPanel", () => {
  beforeEach(() => {
    useAppStore.setState({
      processes: {},
      gateway: { healthy: true, process: "gateway", started: true, ts: 1 },
      hub: {
        healthy: true,
        process: "bus_server",
        pid: 42,
        components: {
          proxy: { ok: true, last_forwarded_s: 1, nested: { lane: "control" } },
        },
      },
    });
  });

  it("renders hub components with hierarchical detail rows", () => {
    const { container } = render(<HealthPanel />);
    fireEvent.click(screen.getByText("1 个 Hub 组件"));

    const text = container.textContent || "";
    expect(text).toContain("proxy");
    expect(text).toContain("last_forwarded_s");
    expect(text).toContain("nested"); // 嵌套对象渲染为分组标题
    expect(text).toContain("lane");
    expect(text).toContain("control");
    expect(text).not.toContain('{"lane":"control"}'); // 不再整体 JSON 序列化
    expect(text).toContain("PID 42");
  });

  it("places local model control after Gateway and Bus Hub but before processes", () => {
    useAppStore.setState({
      processes: {
        yuki: {
          process: "yuki",
          healthy: true,
          fresh: true,
          last_seen_age_s: 0,
          components: {},
        },
      },
    });

    const { container } = render(<HealthPanel />);
    const text = container.textContent || "";

    expect(text.indexOf("Bus Hub")).toBeLessThan(text.indexOf("本地对话模型"));
    expect(text.indexOf("本地对话模型")).toBeLessThan(text.indexOf("yuki"));
  });
});
