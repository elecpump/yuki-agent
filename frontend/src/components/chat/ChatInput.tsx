import { Button } from "antd";
import { useState, type KeyboardEvent } from "react";

interface ChatInputProps {
  disabled: boolean;
  onSend: (text: string) => boolean;
}

export function ChatInput({ disabled, onSend }: ChatInputProps) {
  const [value, setValue] = useState("");

  const send = () => {
    if (onSend(value)) setValue("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!disabled) send();
    }
  };

  return (
    <div className="chat-composer">
      <div className="chat-composer-inner">
        <div className="composer-box">
          <textarea
            aria-label="给 Yuki 发消息"
            placeholder={disabled ? "等待当前请求结束…" : "和 Yuki 说点什么…"}
            value={value}
            rows={1}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={onKeyDown}
          />
          <Button type="primary" disabled={disabled || !value.trim()} onClick={send}>
            发送
          </Button>
        </div>
        <div className="composer-hint">Enter 发送 · Shift + Enter 换行</div>
      </div>
    </div>
  );
}
