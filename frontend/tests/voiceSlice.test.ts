import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../src/state/store";

describe("voice slice", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    useAppStore.setState({
      voiceStatus: null,
      voicePending: false,
      voiceError: null,
    });
  });

  it("refreshes status without presenting a control operation as pending", async () => {
    const status = {
      available: true,
      state: "idle" as const,
      session_id: null,
      active: false,
      hotkey: null,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(status), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const pending = useAppStore.getState().refreshVoice();
    expect(useAppStore.getState().voicePending).toBe(false);
    await pending;

    expect(useAppStore.getState()).toMatchObject({
      voiceStatus: status,
      voicePending: false,
      voiceError: null,
    });
  });

  it("starts listening without changing status optimistically", async () => {
    const idle = {
      available: true,
      state: "idle" as const,
      session_id: null,
      active: false,
      hotkey: null,
    };
    const listening = {
      available: true,
      state: "listening" as const,
      session_id: 1,
      active: true,
      hotkey: null,
    };
    useAppStore.setState({ voiceStatus: idle });
    let resolveRequest: (response: Response) => void = () => undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          resolveRequest = resolve;
        }),
      ),
    );

    const pending = useAppStore.getState().startVoice();
    expect(useAppStore.getState()).toMatchObject({ voiceStatus: idle, voicePending: true });
    resolveRequest(
      new Response(JSON.stringify(listening), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await pending;

    expect(useAppStore.getState()).toMatchObject({
      voiceStatus: listening,
      voicePending: false,
      voiceError: null,
    });
  });

  it("keeps the active status when cancellation fails", async () => {
    const listening = {
      available: true,
      state: "listening" as const,
      session_id: 1,
      active: true,
      hotkey: null,
    };
    useAppStore.setState({ voiceStatus: listening });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Gateway 不可达")));

    await useAppStore.getState().cancelVoice();

    expect(useAppStore.getState()).toMatchObject({
      voiceStatus: listening,
      voicePending: false,
      voiceError: "Gateway 不可达",
    });
  });
});
