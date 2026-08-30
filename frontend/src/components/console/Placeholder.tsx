export function Placeholder({ title }: { title: string }) {
  return (
    <div className="placeholder">
      <div><div style={{ fontSize: 28, marginBottom: 10 }}>◇</div>{title}<br />v2 即将上线</div>
    </div>
  );
}
