import { useEffect } from "react";
import { getHealth } from "../../api/rest";
import { connections } from "../../api/ws/channels";
import type { HealthMessage } from "../../api/ws/types";
import { STATUS_REST_INTERVAL_MS, STATUS_STALE_AFTER_MS } from "../../config/runtime";
import { useAppStore } from "../store";

export function useHealthStream(): void {
  useEffect(() => {
    let disposed = false;
    let requestInFlight = false;
    let lastProbeAt = 0;

    const probe = async () => {
      if (requestInFlight || disposed) return;
      requestInFlight = true;
      lastProbeAt = Date.now();
      try {
        const snapshot = await getHealth();
        if (!disposed) useAppStore.getState().applyRestSnapshot(snapshot);
      } catch (error) {
        if (!disposed) {
          useAppStore
            .getState()
            .setRestFailure(error instanceof Error ? error.message : "Gateway 不可达");
        }
      } finally {
        requestInFlight = false;
      }
    };

    const removeMessage = connections.onMessage(({ channel, message }) => {
      if (channel !== "status") return;
      const state = useAppStore.getState();
      state.markChannelMessage("status");
      if (message.type === "health") state.applyWsSnapshot((message as HealthMessage).data);
    });
    const removeState = connections.onState(({ channel, state }) => {
      if (channel !== "status") return;
      useAppStore.getState().setChannelState("status", state);
      if (state === "reconnecting" || state === "closed") void probe();
    });

    connections.connect("status");
    void probe();
    const restInterval = setInterval(() => void probe(), STATUS_REST_INTERVAL_MS);
    const staleCheck = setInterval(() => {
      const { statusLastMessageAt } = useAppStore.getState();
      const now = Date.now();
      if (
        statusLastMessageAt !== null &&
        now - statusLastMessageAt > STATUS_STALE_AFTER_MS &&
        now - lastProbeAt > 5_000
      ) {
        void probe();
      }
    }, 5_000);

    return () => {
      disposed = true;
      clearInterval(restInterval);
      clearInterval(staleCheck);
      removeMessage();
      removeState();
      connections.disconnect("status");
    };
  }, []);
}
