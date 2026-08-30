import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../src/state/store";
import type { HealthSnapshot } from "../src/types/api";

function snapshot(hub: HealthSnapshot["hub"]): HealthSnapshot {
  return {
    gateway: { healthy: true, process: "gateway", started: true, ts: 1 },
    hub,
    processes: {
      yuki: {
        process: "yuki",
        healthy: true,
        fresh: true,
        last_seen_age_s: 1,
        components: {},
      },
    },
  };
}

describe("health slice", () => {
  beforeEach(() => {
    useAppStore.setState({
      processes: {},
      hub: null,
      gateway: null,
      statusLastMessageAt: null,
      chatLastMessageAt: null,
      restReachable: null,
      lastRestSuccessAt: null,
      healthError: null,
    });
  });

  it("does not overwrite an authoritative hub with cached websocket data", () => {
    useAppStore.getState().applyRestSnapshot(snapshot({ healthy: true, process: "bus_server" }), 10);
    useAppStore.getState().applyWsSnapshot(snapshot({ healthy: null, cached: true }), 20);

    expect(useAppStore.getState().hub).toMatchObject({ healthy: true, process: "bus_server" });
    expect(useAppStore.getState().statusLastMessageAt).toBe(20);
  });

  it("keeps status and chat message timestamps separate", () => {
    useAppStore.getState().markChannelMessage("status", 10);
    useAppStore.getState().markChannelMessage("chat", 30);
    expect(useAppStore.getState().statusLastMessageAt).toBe(10);
    expect(useAppStore.getState().chatLastMessageAt).toBe(30);
  });
});
