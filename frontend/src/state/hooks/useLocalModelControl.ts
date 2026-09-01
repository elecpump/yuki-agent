import { useCallback, useEffect, useRef } from "react";
import { getLocalModelOperation } from "../../api/rest";
import { LOCAL_MODEL_OPERATION_POLL_MS } from "../../config/runtime";
import type { LocalModelStatus } from "../../types/api";
import { useAppStore } from "../store";

export interface LocalModelControl {
  status: LocalModelStatus | null;
  loading: boolean;
  operationId: string | null;
  error: string | null;
  refresh: () => Promise<void>;
  setEnabled: (enabled: boolean) => Promise<void>;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "本地模型操作失败";
}

export function useLocalModelControl(): LocalModelControl {
  const status = useAppStore((state) => state.localModelStatus);
  const loading = useAppStore((state) => state.localModelLoading);
  const operationId = useAppStore((state) => state.localModelOperationId);
  const error = useAppStore((state) => state.localModelError);
  const disposed = useRef(false);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const requestController = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (disposed.current) return;
    const controller = new AbortController();
    requestController.current = controller;
    await useAppStore.getState().refreshLocalModel(controller.signal);
  }, []);

  const finish = useCallback(async (terminalError: string | null) => {
    if (disposed.current) return;
    await refresh();
    if (disposed.current) return;
    useAppStore.setState({
      localModelLoading: false,
      localModelOperationId: null,
      localModelError: terminalError,
    });
  }, [refresh]);

  const poll = useCallback((id: string) => {
    const schedule = (operationId: string) => {
      pollTimer.current = setTimeout(
        () => void run(operationId),
        LOCAL_MODEL_OPERATION_POLL_MS,
      );
    };
    const run = async (operationId: string) => {
      if (disposed.current) return;
      const controller = new AbortController();
      requestController.current = controller;
      try {
        const operation = await getLocalModelOperation(operationId, controller.signal);
        if (disposed.current) return;
        if (operation.state === "succeeded") {
          await finish(null);
        } else if (operation.state === "failed") {
          await finish(operation.error_code || "本地模型操作失败");
        } else {
          schedule(operation.operation_id);
        }
      } catch (pollError) {
        const message = errorMessage(pollError);
        await refresh();
        if (disposed.current) return;
        const authoritative = useAppStore.getState().localModelStatus?.operation;
        if (
          authoritative
          && authoritative.state !== "succeeded"
          && authoritative.state !== "failed"
        ) {
          useAppStore.setState({
            localModelLoading: true,
            localModelOperationId: authoritative.operation_id,
            localModelError: message,
          });
          schedule(authoritative.operation_id);
        } else {
          useAppStore.setState({
            localModelLoading: false,
            localModelOperationId: null,
            localModelError: message,
          });
        }
      }
    };
    schedule(id);
  }, [finish, refresh]);

  useEffect(() => {
    disposed.current = false;
    void refresh().then(() => {
      if (disposed.current) return;
      const state = useAppStore.getState();
      if (state.localModelError !== null) return;
      const operation = state.localModelStatus?.operation;
      if (operation && operation.state !== "succeeded" && operation.state !== "failed") {
        useAppStore.setState({
          localModelLoading: true,
          localModelOperationId: operation.operation_id,
        });
        poll(operation.operation_id);
      }
    });
    return () => {
      disposed.current = true;
      if (pollTimer.current !== null) clearTimeout(pollTimer.current);
      requestController.current?.abort();
    };
  }, [poll, refresh]);

  const setEnabled = useCallback(async (enabled: boolean) => {
    if (useAppStore.getState().localModelLoading) return;
    try {
      const id = await useAppStore
        .getState()
        .setLocalModelEnabled(enabled, crypto.randomUUID());
      if (!disposed.current) poll(id);
    } catch (submitError) {
      await finish(errorMessage(submitError));
    }
  }, [finish, poll]);

  return { status, loading, operationId, error, refresh, setEnabled };
}
