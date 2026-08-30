import type { ChatMessage } from "../../state/slices/chatSlice";
import { ReasonBadge } from "./ReasonBadge";

export function MessageBubble({ message }: { message: ChatMessage }) {
  return (
    <div className={`message-row ${message.role}`} data-testid={`message-${message.role}`}>
      <div className="message-bubble">
        <div className="message-content">{message.text}</div>
        {message.role === "assistant" && (
          <div className="message-meta">
            <ReasonBadge reason={message.reason} status={message.status} />
            {message.emotion && <span className="reason-badge">{message.emotion}</span>}
          </div>
        )}
      </div>
    </div>
  );
}
