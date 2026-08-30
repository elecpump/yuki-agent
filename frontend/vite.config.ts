import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          antd: ["antd"],
          react: ["react", "react-dom", "zustand"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./tests/setup.ts",
  },
});
