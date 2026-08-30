import type { StateCreator } from "zustand";

export type ConsoleTab = "health" | "memory" | "soul" | "config" | "perception";

export interface UiSlice {
  consoleOpen: boolean;
  activeTab: ConsoleTab;
  toggleConsole: () => void;
  setTab: (tab: ConsoleTab) => void;
}

export const createUiSlice: StateCreator<UiSlice, [], [], UiSlice> = (set) => ({
  consoleOpen: true,
  activeTab: "health",
  toggleConsole: () => set((state) => ({ consoleOpen: !state.consoleOpen })),
  setTab: (activeTab) => set({ activeTab }),
});
