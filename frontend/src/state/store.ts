import { create } from "zustand";
import { createChatSlice, type ChatSlice } from "./slices/chatSlice";
import { createHealthSlice, type HealthSlice } from "./slices/healthSlice";
import { createUiSlice, type UiSlice } from "./slices/uiSlice";

export type AppState = ChatSlice & HealthSlice & UiSlice;

export const useAppStore = create<AppState>()((...args) => ({
  ...createChatSlice(...args),
  ...createHealthSlice(...args),
  ...createUiSlice(...args),
}));
