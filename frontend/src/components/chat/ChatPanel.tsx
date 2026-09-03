import { Button } from "antd";
import { useEffect, useRef } from "react";
import { useAppStore } from "../../state/store";
import type { VoiceControl } from "../../state/hooks/useVoiceControl";
import { ChatInput } from "./ChatInput";
import { MessageBubble } from "./MessageBubble";
import { ThinkingIndicator } from "./ThinkingIndicator";

interface ChatPanelProps {
  onSend: (text: string) => boolean;
  onCancel: () => void;
  voice: VoiceControl;
}

export function ChatPanel({ onSend, onCancel, voice }: ChatPanelProps) {
  const messages = useAppStore((state) => state.messages);
  const pending = useAppStore((state) => state.pending);
  const sendLocked = useAppStore((state) => state.sendLocked);
  const chatHistoryLoading = useAppStore((state) => state.chatHistoryLoading);
  const chatWsState = useAppStore((state) => state.chatWsState);
  const requestMayBeQueued = useAppStore((state) => state.requestMayBeQueued);
  const error = useAppStore((state) => state.chatError);
  const toggleConsole = useAppStore((state) => state.toggleConsole);
  const listRef = useRef<HTMLDivElement>(null);
  let disabledReason: string | undefined;
  if (chatHistoryLoading) disabledReason = "正在加载聊天记录，输入内容会保留";
  else if (sendLocked) disabledReason = "等待当前请求结束…";
  else if (chatWsState !== "open") disabledReason = "对话通道连接中，输入内容会保留";

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, pending]);

  return (
    <main className="chat-panel">
      <header className="chat-header">
        <div className="brand">
          <div className="brand-mark">Y</div>
          <div>
            <div className="brand-title">Yuki</div>
            <div className="brand-subtitle">桌面陪伴助手</div>
          </div>
        </div>
        <Button type="text" onClick={toggleConsole} aria-label="切换控制台">
          状态
        </Button>
      </header>
      <div className="message-list" ref={listRef}>
        <div className="message-list-inner">
          {messages.length === 0 ? (
            <div className="empty-chat">
              <h1>晚上好。</h1>
              <p>我在这里。可以聊聊你正在看的内容，也可以只是说说今天发生了什么。</p>
            </div>
          ) : (
            messages.map((message) => <MessageBubble key={message.id} message={message} />)
          )}
          {pending && (
            <ThinkingIndicator onCancel={onCancel} mayBeQueued={requestMayBeQueued} />
          )}
        </div>
      </div>
      {error && <div className="chat-composer-inner chat-error">{error}</div>}
      <ChatInput
        disabled={chatHistoryLoading || sendLocked || chatWsState !== "open"}
        disabledReason={disabledReason}
        onSend={onSend}
        voiceStatus={voice.status}
        voicePending={voice.pending}
        voiceError={voice.error}
        onToggleVoice={() => void voice.toggleVoice()}
      />
    </main>
  );
}
