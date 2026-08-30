import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageBubble } from "../src/components/chat/MessageBubble";

describe("MessageBubble", () => {
  it("renders an open reason code", () => {
    render(
      <MessageBubble
        message={{
          id: "1",
          role: "assistant",
          text: "你好",
          reason: "future_reason",
          createdAt: 1,
        }}
      />,
    );
    expect(screen.getByText("future_reason")).toBeInTheDocument();
  });
});
