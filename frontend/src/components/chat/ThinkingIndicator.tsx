import { Button } from "antd";

export function ThinkingIndicator({ onCancel }: { onCancel: () => void }) {
  return (
    <div className="thinking" role="status">
      <div className="thinking-dots" aria-label="Yuki 正在思考">
        <span />
        <span />
        <span />
      </div>
      <span>Yuki 正在思考</span>
      <Button size="small" danger type="text" onClick={onCancel}>
        取消
      </Button>
    </div>
  );
}
