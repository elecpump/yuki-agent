import { Button } from "antd";

interface ThinkingIndicatorProps {
  onCancel: () => void;
  mayBeQueued: boolean;
}

export function ThinkingIndicator({ onCancel, mayBeQueued }: ThinkingIndicatorProps) {
  return (
    <div className="thinking" role="status">
      <div className="thinking-dots" aria-label="Yuki 正在思考">
        <span />
        <span />
        <span />
      </div>
      <span>
        {mayBeQueued ? "正在等待上一请求结束，然后继续思考" : "Yuki 正在思考"}
      </span>
      <Button size="small" danger type="text" onClick={onCancel}>
        取消
      </Button>
    </div>
  );
}
