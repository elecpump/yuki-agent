import type { StateCreator } from "zustand";
import {
  getLocalModelStatus,
  setLocalModelEnabled as requestLocalModelEnabled,
} from "../../api/rest";
import type { LocalModelStatus } from "../../types/api";

export interface LocalModelSlice {
  localModelStatus: LocalModelStatus | null;
  localModelLoading: boolean;
  localModelOperationId: string | null;
  localModelError: string | null;
  refreshLocalModel: (signal?: AbortSignal) => Promise<void>;
  setLocalModelEnabled: (
    enabled: boolean,
    idempotencyKey: string,
    signal?: AbortSignal,
  ) => Promise<string>;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "无法读取本地模型状态";
}

export const createLocalModelSlice: StateCreator<
  LocalModelSlice,
  [],
  [],
  LocalModelSlice
> = (set) => ({
  localModelStatus: null,
  localModelLoading: false,
  localModelOperationId: null,
  localModelError: null,

  refreshLocalModel: async (signal) => {
    set({ localModelLoading: true });
    try {
      const status = await getLocalModelStatus(signal);
      set({ localModelStatus: status, localModelLoading: false, localModelError: null });
    } catch (error) {
      set({ localModelLoading: false, localModelError: errorMessage(error) });
    }
  },

  setLocalModelEnabled: async (enabled, idempotencyKey, signal) => {
    set({ localModelLoading: true, localModelError: null });
    try {
      const accepted = await requestLocalModelEnabled(enabled, idempotencyKey, signal);
      set({ localModelOperationId: accepted.operation_id });
      return accepted.operation_id;
    } catch (error) {
      set({ localModelLoading: false, localModelError: errorMessage(error) });
      throw error;
    }
  },
});
