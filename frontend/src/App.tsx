import { ChatPanel } from "./components/chat/ChatPanel";
import { ConsoleDrawer } from "./components/console/ConsoleDrawer";
import { useChat } from "./state/hooks/useChat";
import { useHealthStream } from "./state/hooks/useHealthStream";

export default function App() {
  useHealthStream();
  const chat = useChat();
  return (
    <div className="app-shell">
      <ChatPanel onSend={chat.sendMessage} onCancel={chat.cancel} />
      <ConsoleDrawer />
    </div>
  );
}
