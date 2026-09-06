export default function RunningStateDot() {
  return (
    <span
      className="status-dot running-state-dot"
      aria-label="Running"
      title="Running"
    />
  );
}

export function RunningStatus() {
  return (
    <span className="running-state-status">
      <RunningStateDot />
      <span aria-hidden="true">running</span>
    </span>
  );
}
