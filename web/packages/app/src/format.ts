export function formatTimestamp(timestamp: number | null | undefined) {
  if (!timestamp) return '暂无';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(new Date(timestamp * 1000));
}

export function formatHour(timestamp: number) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(new Date(timestamp * 1000));
}

export function formatNumber(value: number | null, digits = 0) {
  return value === null ? '暂无' : value.toFixed(digits);
}
