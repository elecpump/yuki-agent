import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatInput } from "../src/components/chat/ChatInput";

afterEach(cleanup);

describe("ChatInput", () => {
  it("keeps draft text editable while the chat channel is unavailable", () => {
    const onSend = vi.fn(() => true);
    render(
      <ChatInput
        disabled
        disabledReason="对话通道连接中，输入内容会保留"
        onSend={onSend}
        voiceStatus={{ available: true, state: "idle", session_id: null, active: false, hotkey: null }}
        voicePending={false}
        voiceError={null}
        onToggleVoice={vi.fn()}
      />,
    );

    const input = screen.getByLabelText("给 Yuki 发消息");
    fireEvent.change(input, { target: { value: "先写下来" } });
    fireEvent.keyDown(input, { key: "Enter" });

    expect(input).toHaveValue("先写下来");
    expect(screen.getByRole("button", { name: "发 送" })).toBeDisabled();
    expect(screen.getByText("对话通道连接中，输入内容会保留")).toBeInTheDocument();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("starts voice input without depending on the chat channel", () => {
    const onToggleVoice = vi.fn();
    render(
      <ChatInput
        disabled
        disabledReason="对话通道连接中，输入内容会保留"
        onSend={vi.fn(() => false)}
        voiceStatus={{ available: true, state: "idle", session_id: null, active: false, hotkey: null }}
        voicePending={false}
        voiceError={null}
        onToggleVoice={onToggleVoice}
      />,
    );

    const voiceButton = screen.getByRole("button", { name: "开始语音" });
    expect(voiceButton).toBeEnabled();
    expect(voiceButton).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(voiceButton);
    expect(onToggleVoice).toHaveBeenCalledOnce();
  });

  it("offers cancellation while the backend voice session is active", () => {
    render(
      <ChatInput
        disabled={false}
        onSend={vi.fn(() => false)}
        voiceStatus={{ available: true, state: "speaking", session_id: 3, active: true, hotkey: null }}
        voicePending={false}
        voiceError={null}
        onToggleVoice={vi.fn()}
      />,
    );

    const voiceButton = screen.getByRole("button", { name: "取消语音" });
    expect(voiceButton).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("正在聆听你的声音…")).toBeInTheDocument();
  });

  it("disables voice control while Yuki is speaking", () => {
    render(
      <ChatInput
        disabled={false}
        onSend={vi.fn(() => false)}
        voiceStatus={{ available: true, state: "tts", session_id: null, active: false, hotkey: null }}
        voicePending={false}
        voiceError={null}
        onToggleVoice={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "开始语音" })).toBeDisabled();
    expect(screen.getByText("Yuki 正在说话…")).toBeInTheDocument();
  });

  it("explains why voice control is unavailable", () => {
    render(
      <ChatInput
        disabled={false}
        onSend={vi.fn(() => false)}
        voiceStatus={{ available: false, state: "idle", session_id: null, active: false, hotkey: null }}
        voicePending={false}
        voiceError={null}
        onToggleVoice={vi.fn()}
      />,
    );

    const voiceButton = screen.getByRole("button", { name: "开始语音" });
    expect(voiceButton).toBeDisabled();
    expect(voiceButton).toHaveAttribute("title", "语音功能不可用");
  });

  it("shows whether the global hotkey is active or using the window fallback", () => {
    const props = {
      disabled: false,
      onSend: vi.fn(() => false),
      voicePending: false,
      voiceError: null,
      onToggleVoice: vi.fn(),
    };
    const { rerender } = render(
      <ChatInput
        {...props}
        voiceStatus={{
          available: true,
          state: "idle",
          session_id: null,
          active: false,
          hotkey: { registered: true, error: "" },
        }}
      />,
    );
    expect(screen.getByText("全局热键已启用（Ctrl+Shift+Space）")).toBeInTheDocument();

    rerender(
      <ChatInput
        {...props}
        voiceStatus={{
          available: true,
          state: "idle",
          session_id: null,
          active: false,
          hotkey: { registered: false, error: "shortcut occupied" },
        }}
      />,
    );
    expect(
      screen.getByText("全局热键不可用（被占用），请点击语音按钮"),
    ).toBeInTheDocument();
  });
});
