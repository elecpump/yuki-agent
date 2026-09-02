import { create } from "zustand";
import { createChatSlice, type ChatSlice } from "./slices/chatSlice";
import { createHealthSlice, type HealthSlice } from "./slices/healthSlice";
import { createLocalModelSlice, type LocalModelSlice } from "./slices/localModelSlice";
import { createUiSlice, type UiSlice } from "./slices/uiSlice";
import { createVoiceSlice, type VoiceSlice } from "./slices/voiceSlice";

export type AppState = ChatSlice & HealthSlice & LocalModelSlice & UiSlice & VoiceSlice;

export const useAppStore = create<AppState>()((...args) => ({
  ...createChatSlice(...args),
  ...createHealthSlice(...args),
  ...createLocalModelSlice(...args),
  ...createUiSlice(...args),
  ...createVoiceSlice(...args),
}));
