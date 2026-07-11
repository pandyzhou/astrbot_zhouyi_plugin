import { useId, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from 'react';
import type { TrendPoint } from '../../api/types';
import { formatHour, formatTimestamp } from '../../format';

interface TrendChartProps {
  serverName: string;
  hours: number;
  points: TrendPoint[];
}

type ActiveSample = {
  index: number;
  source: 'keyboard' | 'pointer' | 'touch';
};

const width = 760;
const height = 260;
const padding = { top: 22, right: 18, bottom: 42, left: 44 };

export function TrendChart({ serverName, hours, points }: TrendChartProps) {
  const tooltipId = useId();
  const [activeSample, setActiveSample] = useState<ActiveSample | null>(null);
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
  const ariaLabel = `${serverName} 最近 ${hours} 小时在线人数趋势，共 ${values.length} 个有效采样，${missingCount} 个缺失采样；缺失采样不代表在线人数为零。可使用左右方向键查看各整点数据。`;
  const activePoint = activeSample === null ? null : points[activeSample.index];
  const activeX = activeSample === null ? 0 : x(activeSample.index);
  const activeY = activePoint?.players === null || activePoint === null ? padding.top + plotHeight / 2 : y(activePoint.players);
  const tooltipStyle = activePoint === null ? undefined : {
    '--trend-tooltip-x': `${activeX / width * 100}%`,
    '--trend-tooltip-y': `${activeY / height * 100}%`,
  } as CSSProperties;
  const tooltipClassName = [
    'trend-tooltip',
    activeX < width * 0.3 ? 'trend-tooltip--align-left' : '',
    activeX > width * 0.7 ? 'trend-tooltip--align-right' : '',
    activeY < height * 0.42 ? 'trend-tooltip--below' : '',
  ].filter(Boolean).join(' ');

  function indexFromPointer(event: PointerEvent<SVGSVGElement>) {
    if (!points.length) return null;
    const bounds = event.currentTarget.getBoundingClientRect();
    if (!bounds.width) return null;
    const svgX = (event.clientX - bounds.left) / bounds.width * width;
    const ratio = Math.max(0, Math.min(1, (svgX - padding.left) / plotWidth));
    return points.length === 1 ? 0 : Math.round(ratio * (points.length - 1));
  }

  function activatePointerSample(event: PointerEvent<SVGSVGElement>) {
    const index = indexFromPointer(event);
    if (index === null) return;
    const source = event.pointerType === 'touch' ? 'touch' : 'pointer';
    setActiveSample((currentSample) => (
      currentSample?.source === source && currentSample.index === index
        ? currentSample
        : { index, source }
    ));
  }

  function handlePointerMove(event: PointerEvent<SVGSVGElement>) {
    if (event.pointerType !== 'touch') activatePointerSample(event);
  }

  function handleKeyDown(event: KeyboardEvent<SVGSVGElement>) {
    if (!points.length) return;
    if (event.key === 'Escape') {
      setActiveSample(null);
      return;
    }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const direction = event.key === 'ArrowLeft' ? -1 : 1;
    setActiveSample((currentSample) => {
      const currentIndex = currentSample?.index ?? points.length - 1;
      return {
        index: Math.max(0, Math.min(points.length - 1, currentIndex + direction)),
        source: 'keyboard',
      };
    });
  }

  function activateLatestSample() {
    if (!points.length) return;
    setActiveSample((currentSample) => currentSample ?? { index: points.length - 1, source: 'keyboard' });
  }

  return (
    <div className="trend-chart-block">
      <div className="chart-frame">
        <svg
          className="trend-chart"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={ariaLabel}
          aria-describedby={activePoint === null ? undefined : tooltipId}
          preserveAspectRatio="none"
          tabIndex={0}
          onBlur={() => setActiveSample(null)}
          onFocus={activateLatestSample}
          onKeyDown={handleKeyDown}
          onPointerDown={activatePointerSample}
          onPointerLeave={() => setActiveSample((currentSample) => currentSample?.source === 'pointer' ? null : currentSample)}
          onPointerMove={handlePointerMove}
        >
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
          {activePoint === null ? null : (
            <g className="chart-active-sample" aria-hidden="true">
              <line x1={activeX} y1={padding.top} x2={activeX} y2={padding.top + plotHeight} />
              {activePoint.players === null ? null : <circle cx={activeX} cy={activeY} r="6" />}
            </g>
          )}
          <g className="chart-x-labels" aria-hidden="true">
            {labelIndexes.map((index) => (
              <text key={index} x={x(index)} y={height - 13} textAnchor={index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle'}>
                {formatHour(points[index].timestamp)}
              </text>
            ))}
          </g>
        </svg>
        {activePoint === null ? null : (
          <div className={tooltipClassName} id={tooltipId} role="tooltip" style={tooltipStyle}>
            <strong>{formatTimestamp(activePoint.timestamp)}</strong>
            <span className="trend-tooltip__server">{serverName}</span>
            <dl>
              <div>
                <dt>在线人数</dt>
                <dd>{activePoint.players ?? '缺失采样'}</dd>
              </div>
            </dl>
          </div>
        )}
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
    </div>
  );
}
