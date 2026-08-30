import type { StateCreator } from "zustand";
import type { ChannelName, ChannelState } from "../../api/ws/types";
import type { GatewayHealth, HealthSnapshot, HubHealth, ProcessHealth } from "../../types/api";

export interface HealthSlice {
  processes: Record<string, ProcessHealth>;
  hub: HubHealth | null;
  gateway: GatewayHealth | null;
  statusWsState: ChannelState;
  statusLastMessageAt: number | null;
  chatWsState: ChannelState;
  chatLastMessageAt: number | null;
  restReachable: boolean | null;
  lastRestSuccessAt: number | null;
  healthError: string | null;
  applyWsSnapshot: (snapshot: HealthSnapshot, receivedAt?: number) => void;
  applyRestSnapshot: (snapshot: HealthSnapshot, receivedAt?: number) => void;
  markChannelMessage: (channel: ChannelName, receivedAt?: number) => void;
  setChannelState: (channel: ChannelName, state: ChannelState) => void;
  setRestFailure: (message: string) => void;
}

export const createHealthSlice: StateCreator<HealthSlice, [], [], HealthSlice> = (set) => ({
  processes: {},
  hub: null,
  gateway: null,
  statusWsState: "idle",
  statusLastMessageAt: null,
  chatWsState: "idle",
  chatLastMessageAt: null,
  restReachable: null,
  lastRestSuccessAt: null,
  healthError: null,

  applyWsSnapshot: (snapshot, receivedAt = Date.now()) =>
    set((state) => ({
      processes: snapshot.processes,
      gateway: snapshot.gateway,
      hub: snapshot.hub.cached ? state.hub : snapshot.hub,
      statusLastMessageAt: receivedAt,
    })),

  applyRestSnapshot: (snapshot, receivedAt = Date.now()) =>
    set({
      processes: snapshot.processes,
      gateway: snapshot.gateway,
      hub: snapshot.hub,
      restReachable: true,
      lastRestSuccessAt: receivedAt,
      healthError: null,
    }),

  markChannelMessage: (channel, receivedAt = Date.now()) =>
    set(channel === "status" ? { statusLastMessageAt: receivedAt } : { chatLastMessageAt: receivedAt }),

  setChannelState: (channel, state) =>
    set(channel === "status" ? { statusWsState: state } : { chatWsState: state }),

  setRestFailure: (message) => set({ restReachable: false, healthError: message }),
});
