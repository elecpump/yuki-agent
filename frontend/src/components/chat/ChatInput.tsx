import { Button, Tooltip } from "antd";
import { useState, type KeyboardEvent } from "react";
import type { VoiceStatus } from "../../types/api";

interface ChatInputProps {
  disabled: boolean;
  disabledReason?: string;
  onSend: (text: string) => boolean;
  voiceStatus: VoiceStatus | null;
  voicePending: boolean;
  voiceError: string | null;
  onToggleVoice: () => void;
}

function voiceHint(status: VoiceStatus | null): string | undefined {
  if (!status) return "正在连接语音服务…";
  if (!status.available) return "语音功能不可用";
  return {
    idle: undefined,
    listening: "正在聆听…",
    speaking: "正在聆听你的声音…",
    processing: "正在识别…",
    tts: "Yuki 正在说话…",
  }[status.state];
}

function voiceTooltip(status: VoiceStatus | null, error: string | null): string {
  if (!status?.available) {
    return error || (status ? "语音功能不可用" : "正在连接语音服务");
  }
  if (status.state === "tts") return "Yuki 正在说话";
  if (status.active) return "取消语音（Ctrl+Shift+Space）";
  return "开始语音（Ctrl+Shift+Space）";
}

function hotkeyHint(status: VoiceStatus | null): string | undefined {
  if (status?.hotkey?.registered === true) {
    return "全局热键已启用（Ctrl+Shift+Space）";
  }
  if (status?.hotkey?.registered === false) {
    return "全局热键不可用（被占用），请点击语音按钮";
  }
  return undefined;
}

export function ChatInput({
  disabled,
  disabledReason,
  onSend,
  voiceStatus,
  voicePending,
  voiceError,
  onToggleVoice,
}: ChatInputProps) {
  const [value, setValue] = useState("");
  const voiceActive = Boolean(voiceStatus?.active);
  const voiceDisabled =
    voicePending || !voiceStatus?.available || voiceStatus.state === "tts";
  const voiceLabel = voiceActive ? "取消语音" : "开始语音";
  const voiceTooltipText = voiceTooltip(voiceStatus, voiceError);

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
            placeholder="和 Yuki 说点什么…"
            value={value}
            rows={1}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={onKeyDown}
          />
          <Tooltip title={voiceTooltipText}>
            <span>
              <Button
                type="text"
                className={`voice-button${voiceActive ? " active" : ""}`}
                aria-label={voiceLabel}
                aria-pressed={voiceActive}
                title={voiceTooltipText}
                disabled={voiceDisabled}
                loading={voicePending}
                onClick={onToggleVoice}
              >
                <span aria-hidden="true">🎙</span>
              </Button>
            </span>
          </Tooltip>
          <Button type="primary" disabled={disabled || !value.trim()} onClick={send}>
            发送
          </Button>
        </div>
        <div className="composer-hint">
          {disabledReason
            || voiceError
            || voiceHint(voiceStatus)
            || hotkeyHint(voiceStatus)
            || "Enter 发送 · Shift + Enter 换行 · Ctrl + Shift + Space 语音"}
        </div>
      </div>
    </div>
  );
}
