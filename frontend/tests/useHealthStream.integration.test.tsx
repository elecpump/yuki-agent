import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionStatus } from "../src/components/shell/ConnectionStatus";
import { useHealthStream } from "../src/state/hooks/useHealthStream";
import { useAppStore } from "../src/state/store";
import type { HealthSnapshot } from "../src/types/api";
import { FakeWebSocket } from "./FakeWebSocket";

const health: HealthSnapshot = {
  gateway: { healthy: true, process: "gateway", started: true, ts: 1 },
  hub: { healthy: true, process: "bus_server" },
  processes: {},
};

function Harness() {
  useHealthStream();
  return <ConnectionStatus />;
}

describe("useHealthStream integration", () => {
  const sockets: FakeWebSocket[] = [];

  beforeEach(() => {
    vi.useFakeTimers();
    sockets.length = 0;
    vi.stubGlobal(
      "WebSocket",
      class extends FakeWebSocket {
        constructor(url: string) {
          super(url);
          sockets.push(this);
        }
      },
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(health), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    useAppStore.setState({
      processes: {},
      hub: null,
      gateway: null,
      statusWsState: "idle",
      statusLastMessageAt: null,
      chatWsState: "open",
      chatLastMessageAt: null,
      restReachable: null,
      lastRestSuccessAt: null,
      healthError: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("refreshes REST every 30s and probes again after 75s of status silence", async () => {
    render(<Harness />);
    await act(async () => undefined);
    act(() => {
      sockets[0]?.open();
      sockets[0]?.message({ type: "health", data: health });
    });

    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(fetch).toHaveBeenCalledTimes(3);
    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it("shows status stream failure while REST remains reachable", async () => {
    render(<Harness />);
    await act(async () => undefined);
    act(() => sockets[0]?.open());
    expect(useAppStore.getState().restReachable).toBe(true);

    act(() => sockets[0]?.close());

    expect(screen.getByText("状态流异常")).toBeInTheDocument();
    expect(useAppStore.getState().statusWsState).toBe("reconnecting");
  });
});
