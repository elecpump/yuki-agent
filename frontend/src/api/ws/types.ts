import type { HealthSnapshot } from "../../types/api";

export type ChannelName = "status" | "chat";
export type ChannelState = "idle" | "connecting" | "open" | "reconnecting" | "closed";

export interface HealthMessage {
  type: "health";
  data: HealthSnapshot;
}

export interface PingMessage {
  type: "ping";
  channel: string;
  ts: number;
}

export interface AssistantChunkMessage {
  type: "assistant_chunk";
  task_id: string;
  text: string;
  reason?: string;
  ts?: number | null;
  spoke?: boolean | null;
  emotion?: string | null;
  user_turn_id?: number | null;
  turn_id?: number | null;
  done: true;
  status: "completed" | "failed" | "cancel_requested" | string;
  error?: string;
}

export interface InterruptAckMessage {
  type: "interrupt_ack";
  task: unknown;
}

export interface VoiceTurnMessage {
  type: "voice_turn";
  data: {
    kind: "user" | "reply";
    turn_id: number;
    reply_to_turn_id: number | null;
    text: string;
    ts: number;
  };
}

export type StatusServerMessage = HealthMessage | PingMessage;
export type ChatServerMessage =
  | AssistantChunkMessage
  | VoiceTurnMessage
  | PingMessage
  | InterruptAckMessage;
export type ServerMessage = StatusServerMessage | ChatServerMessage | { type: string; [key: string]: unknown };

export interface ChannelEvent {
  channel: ChannelName;
  generation: number;
  message: ServerMessage;
}

export interface ChannelStateEvent {
  channel: ChannelName;
  generation: number;
  state: ChannelState;
}
