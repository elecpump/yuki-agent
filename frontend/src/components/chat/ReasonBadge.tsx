interface ReasonBadgeProps {
  reason?: string;
  status?: string;
}

export function ReasonBadge({ reason, status }: ReasonBadgeProps) {
  const label = reason || (status && status !== "completed" ? status : "");
  return label ? <span className="reason-badge">{label}</span> : null;
}
