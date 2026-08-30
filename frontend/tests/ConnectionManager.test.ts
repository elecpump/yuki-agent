import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionManager } from "../src/api/ws/ConnectionManager";

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  send(value: string) {
    this.sent.push(value);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  message(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

describe("ConnectionManager", () => {
  const sockets: FakeWebSocket[] = [];

  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeWebSocket);
    sockets.length = 0;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  function manager() {
    return new ConnectionManager(() => {
      const socket = new FakeWebSocket();
      sockets.push(socket);
      return socket as unknown as WebSocket;
    });
  }

  it("sends heartbeats only on configured channels", () => {
    const connections = manager();
    connections.register("status", { url: "ws://status", heartbeatMs: 25_000 });
    connections.register("chat", { url: "ws://chat" });
    connections.connect("status");
    connections.connect("chat");
    sockets[0]?.open();
    sockets[1]?.open();

    vi.advanceTimersByTime(25_000);

    expect(sockets[0]?.sent).toEqual([JSON.stringify({ type: "ping" })]);
    expect(sockets[1]?.sent).toEqual([]);
  });

  it("isolates messages from an old socket generation after restart", () => {
    const connections = manager();
    const messages: string[] = [];
    connections.register("chat", { url: "ws://chat" });
    connections.onMessage(({ message }) => messages.push(message.type));
    connections.connect("chat");
    const oldSocket = sockets[0]!;
    oldSocket.open();
    connections.restart("chat");
    const newSocket = sockets[1]!;
    newSocket.open();

    oldSocket.message({ type: "assistant_chunk" });
    newSocket.message({ type: "assistant_chunk" });

    expect(messages).toEqual(["assistant_chunk"]);
  });

  it("caps reconnect backoff at ten seconds", () => {
    const connections = manager();
    connections.register("chat", { url: "ws://chat" });
    connections.connect("chat");
    for (let index = 0; index < 8; index += 1) {
      sockets.at(-1)?.close();
      vi.runOnlyPendingTimers();
    }
    expect(sockets.length).toBe(9);
    expect(connections.state("chat")).toBe("reconnecting");
  });
});
