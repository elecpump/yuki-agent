import type {
  ChannelEvent,
  ChannelName,
  ChannelState,
  ChannelStateEvent,
  ServerMessage,
} from "./types";

type WebSocketFactory = (url: string) => WebSocket;
type MessageListener = (event: ChannelEvent) => void;
type StateListener = (event: ChannelStateEvent) => void;

interface ChannelConfig {
  url: string;
  heartbeatMs?: number;
}

interface ChannelRuntime {
  config: ChannelConfig;
  socket: WebSocket | null;
  generation: number;
  reconnectAttempt: number;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  heartbeatTimer: ReturnType<typeof setInterval> | null;
  manuallyStopped: boolean;
  state: ChannelState;
}

const MAX_RECONNECT_MS = 10_000;

export class ConnectionManager {
  private channels = new Map<ChannelName, ChannelRuntime>();
  private messageListeners = new Set<MessageListener>();
  private stateListeners = new Set<StateListener>();

  constructor(private readonly socketFactory: WebSocketFactory = (url) => new WebSocket(url)) {}

  register(channel: ChannelName, config: ChannelConfig): void {
    if (this.channels.has(channel)) return;
    this.channels.set(channel, {
      config,
      socket: null,
      generation: 0,
      reconnectAttempt: 0,
      reconnectTimer: null,
      heartbeatTimer: null,
      manuallyStopped: true,
      state: "idle",
    });
  }

  connect(channel: ChannelName): number {
    const runtime = this.requireChannel(channel);
    runtime.manuallyStopped = false;
    if (runtime.socket?.readyState === WebSocket.OPEN || runtime.state === "connecting") {
      return runtime.generation;
    }
    this.clearReconnect(runtime);
    return this.open(channel, runtime);
  }

  restart(channel: ChannelName): number {
    const runtime = this.requireChannel(channel);
    runtime.manuallyStopped = false;
    this.clearReconnect(runtime);
    this.clearHeartbeat(runtime);
    const oldSocket = runtime.socket;
    runtime.socket = null;
    if (oldSocket && oldSocket.readyState < WebSocket.CLOSING) oldSocket.close(1000, "client restart");
    return this.open(channel, runtime);
  }

  disconnect(channel: ChannelName): void {
    const runtime = this.requireChannel(channel);
    runtime.manuallyStopped = true;
    this.clearReconnect(runtime);
    this.clearHeartbeat(runtime);
    const socket = runtime.socket;
    runtime.socket = null;
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "client disconnect");
    this.setState(channel, runtime, "closed");
  }

  disconnectAll(): void {
    for (const channel of this.channels.keys()) this.disconnect(channel);
  }

  send(channel: ChannelName, payload: unknown): boolean {
    const runtime = this.requireChannel(channel);
    if (!runtime.socket || runtime.socket.readyState !== WebSocket.OPEN) return false;
    runtime.socket.send(JSON.stringify(payload));
    return true;
  }

  state(channel: ChannelName): ChannelState {
    return this.requireChannel(channel).state;
  }

  generation(channel: ChannelName): number {
    return this.requireChannel(channel).generation;
  }

  onMessage(listener: MessageListener): () => void {
    this.messageListeners.add(listener);
    return () => this.messageListeners.delete(listener);
  }

  onState(listener: StateListener): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  private open(channel: ChannelName, runtime: ChannelRuntime): number {
    runtime.generation += 1;
    const generation = runtime.generation;
    this.setState(channel, runtime, runtime.reconnectAttempt ? "reconnecting" : "connecting");
    let socket: WebSocket;
    try {
      socket = this.socketFactory(runtime.config.url);
    } catch {
      this.scheduleReconnect(channel, runtime);
      return generation;
    }
    runtime.socket = socket;
    socket.onopen = () => {
      if (!this.isCurrent(runtime, socket, generation)) return;
      runtime.reconnectAttempt = 0;
      this.setState(channel, runtime, "open");
      this.startHeartbeat(channel, runtime, generation);
    };
    socket.onmessage = (event) => {
      if (!this.isCurrent(runtime, socket, generation)) return;
      let message: ServerMessage;
      try {
        message = JSON.parse(String(event.data)) as ServerMessage;
      } catch {
        return;
      }
      for (const listener of this.messageListeners) listener({ channel, generation, message });
    };
    socket.onerror = () => {
      if (this.isCurrent(runtime, socket, generation)) socket.close();
    };
    socket.onclose = () => {
      if (!this.isCurrent(runtime, socket, generation)) return;
      runtime.socket = null;
      this.clearHeartbeat(runtime);
      if (runtime.manuallyStopped) {
        this.setState(channel, runtime, "closed");
      } else {
        this.scheduleReconnect(channel, runtime);
      }
    };
    return generation;
  }

  private scheduleReconnect(channel: ChannelName, runtime: ChannelRuntime): void {
    if (runtime.manuallyStopped || runtime.reconnectTimer) return;
    this.setState(channel, runtime, "reconnecting");
    const delay = Math.min(500 * 2 ** runtime.reconnectAttempt, MAX_RECONNECT_MS);
    runtime.reconnectAttempt += 1;
    runtime.reconnectTimer = setTimeout(() => {
      runtime.reconnectTimer = null;
      if (!runtime.manuallyStopped) this.open(channel, runtime);
    }, delay);
  }

  private startHeartbeat(
    channel: ChannelName,
    runtime: ChannelRuntime,
    generation: number,
  ): void {
    this.clearHeartbeat(runtime);
    if (!runtime.config.heartbeatMs) return;
    runtime.heartbeatTimer = setInterval(() => {
      if (runtime.generation !== generation) return;
      this.send(channel, { type: "ping" });
    }, runtime.config.heartbeatMs);
  }

  private setState(channel: ChannelName, runtime: ChannelRuntime, state: ChannelState): void {
    if (runtime.state === state) return;
    runtime.state = state;
    for (const listener of this.stateListeners) {
      listener({ channel, generation: runtime.generation, state });
    }
  }

  private isCurrent(runtime: ChannelRuntime, socket: WebSocket, generation: number): boolean {
    return runtime.socket === socket && runtime.generation === generation;
  }

  private clearReconnect(runtime: ChannelRuntime): void {
    if (runtime.reconnectTimer) clearTimeout(runtime.reconnectTimer);
    runtime.reconnectTimer = null;
  }

  private clearHeartbeat(runtime: ChannelRuntime): void {
    if (runtime.heartbeatTimer) clearInterval(runtime.heartbeatTimer);
    runtime.heartbeatTimer = null;
  }

  private requireChannel(channel: ChannelName): ChannelRuntime {
    const runtime = this.channels.get(channel);
    if (!runtime) throw new Error(`WebSocket channel is not registered: ${channel}`);
    return runtime;
  }
}
