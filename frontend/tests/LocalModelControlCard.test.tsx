import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LocalModelControlCard } from "../src/components/console/health/LocalModelControlCard";
import { useAppStore } from "../src/state/store";

describe("LocalModelControlCard", () => {
  beforeEach(() => {
    useAppStore.setState({
      localModelStatus: null,
      localModelLoading: false,
      localModelOperationId: null,
      localModelError: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("explains and disables control when local chat is not configured", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            available: false,
            enabled: false,
            target_enabled: false,
            state: "unavailable",
            runtime_state: "disabled",
            loaded: false,
            active_calls: 0,
            operation: null,
            last_error: "",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    render(<LocalModelControlCard />);

    expect(await screen.findByText("配置禁用")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "本地对话模型" })).toBeDisabled();
    expect(screen.getByText("运行时设置；Yuki 重启后恢复配置值")).toBeInTheDocument();
  });

  it("locks an enabled switch as soon as a disable operation is submitted", async () => {
    let acceptOperation!: (response: Response) => void;
    const pendingOperation = new Promise<Response>((resolve) => {
      acceptOperation = resolve;
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            available: true,
            enabled: true,
            target_enabled: true,
            state: "enabled",
            runtime_state: "ready",
            loaded: true,
            active_calls: 0,
            operation: null,
            last_error: "",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockReturnValueOnce(pendingOperation);
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("crypto", { randomUUID: () => "disable-request" });

    render(<LocalModelControlCard />);
    const control = await screen.findByRole("switch", { name: "本地对话模型" });
    expect(control).toBeChecked();

    fireEvent.click(control);
    await waitFor(() => expect(control).toBeDisabled());
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      method: "PUT",
      body: JSON.stringify({ enabled: false, idempotency_key: "disable-request" }),
    });

    acceptOperation(
      new Response(
        JSON.stringify({ operation_id: "op-disable", accepted: true, target_enabled: false }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ),
    );
  });
});
