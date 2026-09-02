import { ChatPanel } from "./components/chat/ChatPanel";
import { ConsoleDrawer } from "./components/console/ConsoleDrawer";
import { useChat } from "./state/hooks/useChat";
import { useHealthStream } from "./state/hooks/useHealthStream";
import { useVoiceControl } from "./state/hooks/useVoiceControl";

export default function App() {
  useHealthStream();
  const chat = useChat();
  const voice = useVoiceControl();
  return (
    <div className="app-shell">
      <ChatPanel onSend={chat.sendMessage} onCancel={chat.cancel} voice={voice} />
      <ConsoleDrawer />
    </div>
  );
}
