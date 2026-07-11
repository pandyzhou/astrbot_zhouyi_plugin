import type { TrendPoint } from '../../api/types';
import { formatHour } from '../../format';

interface TrendChartProps {
  serverName: string;
  hours: number;
  points: TrendPoint[];
}

const width = 760;
const height = 260;
const padding = { top: 22, right: 18, bottom: 42, left: 44 };

export function TrendChart({ serverName, hours, points }: TrendChartProps) {
  const values = points.flatMap((point) => point.players === null ? [] : [point.players]);
  const maxValue = Math.max(1, ...values);
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const x = (index: number) => padding.left + (points.length <= 1 ? plotWidth / 2 : index / (points.length - 1) * plotWidth);
  const y = (value: number) => padding.top + plotHeight - value / maxValue * plotHeight;

  const segments: string[] = [];
  let current = '';
  points.forEach((point, index) => {
    if (point.players === null) {
      if (current) segments.push(current);
      current = '';
      return;
    }
    current += `${current ? ' L' : 'M'} ${x(index).toFixed(1)} ${y(point.players).toFixed(1)}`;
  });
  if (current) segments.push(current);

  const labelIndexes = points.length ? Array.from(new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])) : [];
  const missingCount = points.filter((point) => point.players === null).length;
  const ariaLabel = `${serverName} 最近 ${hours} 小时在线人数趋势，共 ${values.length} 个有效采样，${missingCount} 个缺失采样；缺失采样不代表在线人数为零。`;

  return (
    <div className="trend-chart-block">
      <div className="chart-frame">
        <svg className="trend-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={ariaLabel} preserveAspectRatio="none">
          <g className="chart-grid" aria-hidden="true">
            {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
              const gridY = padding.top + plotHeight * ratio;
              return <line key={ratio} x1={padding.left} y1={gridY} x2={width - padding.right} y2={gridY} />;
            })}
          </g>
          <g className="chart-y-labels" aria-hidden="true">
            {[1, 0.5, 0].map((ratio) => (
              <text key={ratio} x={padding.left - 8} y={y(maxValue * ratio) + 4} textAnchor="end">
                {Math.round(maxValue * ratio)}
              </text>
            ))}
          </g>
          {segments.map((path, index) => <path className="chart-line" d={path} key={index} />)}
          <g className="chart-x-labels" aria-hidden="true">
            {labelIndexes.map((index) => (
              <text key={index} x={x(index)} y={height - 13} textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}>
                {formatHour(points[index].timestamp)}
              </text>
            ))}
          </g>
        </svg>
      </div>
      <div className="wf-sr-only">
        <table>
          <caption>{serverName} 最近 {hours} 小时趋势数据；缺失采样不等于真实 0</caption>
          <thead><tr><th scope="col">整点时间</th><th scope="col">在线人数</th></tr></thead>
          <tbody>
            {points.map((point) => (
              <tr key={point.timestamp}><th scope="row">{formatHour(point.timestamp)}</th><td>{point.players ?? '缺失采样'}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      <details className="trend-raw-data">
        <summary>查看原始整点趋势</summary>
        <div className="trend-table-wrap">
          <table className="trend-table">
            <caption>{serverName} 原始整点采样</caption>
            <thead><tr><th scope="col">时间</th><th scope="col">在线人数</th></tr></thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.timestamp}><th scope="row">{formatHour(point.timestamp)}</th><td>{point.players ?? '缺失'}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
