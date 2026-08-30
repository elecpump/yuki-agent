import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChatInput } from "../src/components/chat/ChatInput";

describe("ChatInput", () => {
  it("keeps draft text editable while the chat channel is unavailable", () => {
    const onSend = vi.fn(() => true);
    render(
      <ChatInput
        disabled
        disabledReason="对话通道连接中，输入内容会保留"
        onSend={onSend}
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
});
