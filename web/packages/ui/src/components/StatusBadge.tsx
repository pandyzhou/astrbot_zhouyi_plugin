export type WorkshopStatus = 'unknown' | 'online' | 'offline';

export interface StatusBadgeProps {
  status: WorkshopStatus;
  label?: string;
}

const defaultLabels: Record<WorkshopStatus, string> = {
  unknown: '未知',
  online: '在线',
  offline: '离线',
};

export function StatusBadge({ status, label = defaultLabels[status] }: StatusBadgeProps) {
  return <span className={`wf-status wf-status--${status}`}>{label}</span>;
}
