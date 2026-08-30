import { websocketUrl } from "../../config/runtime";
import { ConnectionManager } from "./ConnectionManager";

export const connections = new ConnectionManager();

connections.register("status", {
  url: websocketUrl("/ws/status"),
  heartbeatMs: 25_000,
});
connections.register("chat", {
  url: websocketUrl("/ws/chat"),
});
