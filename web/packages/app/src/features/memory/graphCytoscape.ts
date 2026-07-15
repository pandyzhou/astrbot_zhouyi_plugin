import cytoscape from 'cytoscape';
import fcose from 'cytoscape-fcose';
import type { GraphEdge, GraphNode } from './types';

export const GRAPH_WIDTH = 1000;
export const GRAPH_HEIGHT = 640;
export const MAX_DYNAMIC_GRAPH_NODES = 80;
export const MIN_NODE_RADIUS = 22;
export const GRAPH_LAYOUT_PADDING = 32;
export const GRAPH_RELATION_COLORS = ['#68b65b', '#d79a68', '#72a8d7', '#c982c9', '#d7ca68', '#7fc9b5', '#d77968', '#9ea6d9'] as const;

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
const MIN_EDGE_LENGTH = 118;
const MAX_EDGE_LENGTH = 220;
const MIN_NODE_REPULSION = 5_000;
const MAX_NODE_REPULSION = 14_000;
const MIN_EDGE_ELASTICITY = 0.32;
const MAX_EDGE_ELASTICITY = 0.62;

let fcoseRegistered = false;

export interface GraphElementNodeData extends cytoscape.NodeDataDefinition {
  id: string;
  graphNodeId: number;
  label: string;
  fullLabel: string;
  nodeType: string;
  weight: number;
  degree: number;
  radius: number;
  diameter: number;
  labelWidth: number;
  visualWidth: number;
  highlighted: 0 | 1;
  repulsion: number;
}

export interface GraphElementEdgeData extends cytoscape.EdgeDataDefinition {
  id: string;
  source: string;
  target: string;
  edgeIndex: number;
  graphEdgeId: number | null;
  relationType: string;
  relationClass: string;
  relationColor: string;
  memoryId: number | null;
  weight: number;
  idealLength: number;
  elasticity: number;
  visible: 0 | 1;
}

export interface GraphElementsModel {
  elements: cytoscape.ElementDefinition[];
  nodeElements: cytoscape.NodeDefinition[];
  edgeElements: cytoscape.EdgeDefinition[];
  relationColors: ReadonlyMap<string, string>;
  dynamicEnabled: boolean;
  dynamicDegraded: boolean;
}

export interface GraphLayoutOptions {
  animate?: boolean;
  fit?: boolean;
  padding?: number;
}

export interface VisualOverlapOptions {
  gap?: number;
  maxIterations?: number;
  tolerance?: number;
}

export interface VisualOverlapResult {
  iterations: number;
  initialOverlaps: number;
  remainingOverlaps: number;
  movedNodeCount: number;
  totalDisplacement: number;
}

export interface FcoseLayoutOptions extends cytoscape.BaseLayoutOptions {
  name: 'fcose';
  quality: 'default' | 'proof';
  randomize: boolean;
  animate: boolean;
  fit: boolean;
  padding: number;
  nodeDimensionsIncludeLabels: boolean;
  uniformNodeDimensions: boolean;
  packComponents: boolean;
  step: 'all';
  samplingType: boolean;
  sampleSize: number;
  nodeSeparation: number;
  piTol: number;
  nodeRepulsion: (node: cytoscape.NodeSingular) => number;
  idealEdgeLength: (edge: cytoscape.EdgeSingular) => number;
  edgeElasticity: (edge: cytoscape.EdgeSingular) => number;
  nestingFactor: number;
  numIter: number;
  tile: boolean;
  tilingCompareBy: (leftId: string, rightId: string) => number;
  tilingPaddingVertical: number;
  tilingPaddingHorizontal: number;
  gravity: number;
  gravityRangeCompound: number;
  gravityCompound: number;
  gravityRange: number;
  initialEnergyOnIncremental: number;
}

export interface GraphStyleColors {
  nodeFill?: string;
  nodeStroke?: string;
  nodeText?: string;
  accent?: string;
  focus?: string;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function finiteNumber(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function compareNodes(left: GraphNode, right: GraphNode): number {
  return (
    finiteNumber(right.weight) - finiteNumber(left.weight) ||
    finiteNumber(right.degree) - finiteNumber(left.degree) ||
    left.id - right.id
  );
}

function compareIndexedEdges(
  left: { edge: GraphEdge; edgeIndex: number },
  right: { edge: GraphEdge; edgeIndex: number },
): number {
  return (
    left.edge.source - right.edge.source ||
    left.edge.target - right.edge.target ||
    finiteNumber(left.edge.id, Number.MAX_SAFE_INTEGER) - finiteNumber(right.edge.id, Number.MAX_SAFE_INTEGER) ||
    String(left.edge.relation_type ?? 'related').localeCompare(String(right.edge.relation_type ?? 'related')) ||
    finiteNumber(left.edge.memory_id, Number.MAX_SAFE_INTEGER) - finiteNumber(right.edge.memory_id, Number.MAX_SAFE_INTEGER) ||
    left.edgeIndex - right.edgeIndex
  );
}

function shortLabel(value: string): string {
  return value.length > 16 ? `${value.slice(0, 15)}…` : value;
}

function estimateLabelWidth(label: string): number {
  let units = 0;
  for (const character of label) units += character.codePointAt(0)! > 0xff ? 1 : 0.58;
  return clamp(18 + units * 12, 44, 178);
}

function nodeRadius(weight: number, maxWeight: number): number {
  const rawRadius = 16 + 8 * Math.sqrt(Math.max(0, weight) / maxWeight);
  return Math.max(MIN_NODE_RADIUS, rawRadius);
}

function seedPosition(index: number, count: number, visualWidth: number, radius: number): cytoscape.Position {
  const progress = Math.sqrt((index + 0.65) / Math.max(count, 1));
  const angle = index * GOLDEN_ANGLE;
  const horizontalExtent = GRAPH_WIDTH * 0.405;
  const verticalExtent = GRAPH_HEIGHT * 0.390625;
  const halfVisualWidth = Math.max(radius, visualWidth / 2);
  return {
    x: clamp(GRAPH_WIDTH / 2 + Math.cos(angle) * horizontalExtent * progress, halfVisualWidth, GRAPH_WIDTH - halfVisualWidth),
    y: clamp(GRAPH_HEIGHT / 2 + Math.sin(angle) * verticalExtent * progress, radius, GRAPH_HEIGHT - radius),
  };
}

function edgeIdentity(edge: GraphEdge): string {
  return [
    edge.source,
    edge.target,
    edge.id ?? '',
    edge.relation_type ?? 'related',
    edge.memory_id ?? '',
    edge.key ?? '',
  ].join('\u0000');
}

function stableStringHash(value: string): string {
  let hash = 2_166_136_261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16_777_619);
  }
  return (hash >>> 0).toString(36);
}

function visibleEdgeCounts(edges: readonly GraphEdge[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const edge of edges) {
    const identity = edgeIdentity(edge);
    counts.set(identity, (counts.get(identity) ?? 0) + 1);
  }
  return counts;
}

function relationColorMap(edges: readonly GraphEdge[], supplied?: ReadonlyMap<string, string>): ReadonlyMap<string, string> {
  const relations = [...new Set(edges.map((edge) => edge.relation_type || 'related'))].sort();
  return new Map(relations.map((relation, index) => [
    relation,
    supplied?.get(relation) ?? GRAPH_RELATION_COLORS[index % GRAPH_RELATION_COLORS.length],
  ]));
}

function relationClassMap(colors: ReadonlyMap<string, string>): ReadonlyMap<string, string> {
  return new Map([...colors.keys()].sort().map((relation, index) => [relation, `graph-relation-${index}`]));
}

function createGraphElementsModel(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
  visibleEdges: readonly GraphEdge[] = edges,
  suppliedRelationColors?: ReadonlyMap<string, string>,
): GraphElementsModel {
  const rankedNodes = [...nodes].sort(compareNodes);
  const uniqueNodes: GraphNode[] = [];
  const seenNodeIds = new Set<number>();
  for (const node of rankedNodes) {
    if (seenNodeIds.has(node.id)) continue;
    seenNodeIds.add(node.id);
    uniqueNodes.push(node);
  }
  const maxWeight = Math.max(...uniqueNodes.map((node) => Math.max(0, finiteNumber(node.weight))), 1);
  const nodeIds = new Set(uniqueNodes.map((node) => node.id));
  const colors = relationColorMap(edges, suppliedRelationColors);
  const relationClasses = relationClassMap(colors);

  const nodeElements: cytoscape.NodeDefinition[] = uniqueNodes.map((node, index) => {
    const weight = Math.max(0, finiteNumber(node.weight));
    const degree = Math.max(0, finiteNumber(node.degree));
    const fullLabel = String(node.label || `#${node.id}`);
    const label = shortLabel(fullLabel);
    const radius = nodeRadius(weight, maxWeight);
    const diameter = radius * 2;
    const labelWidth = estimateLabelWidth(label);
    const visualWidth = Math.max(diameter, labelWidth);
    const repulsion = clamp(
      MIN_NODE_REPULSION
        + Math.sqrt(degree) * 760
        + Math.log1p(weight) * 620
        + Math.max(0, visualWidth - diameter) * 28,
      MIN_NODE_REPULSION,
      MAX_NODE_REPULSION,
    );
    const data: GraphElementNodeData = {
      id: String(node.id),
      graphNodeId: node.id,
      label,
      fullLabel,
      nodeType: String(node.type ?? 'unknown'),
      weight,
      degree,
      radius,
      diameter,
      labelWidth,
      visualWidth,
      highlighted: node.highlighted ? 1 : 0,
      repulsion,
    };
    return {
      group: 'nodes',
      data,
      position: seedPosition(index, uniqueNodes.length, visualWidth, radius),
      classes: `graph-node${node.highlighted ? ' graph-node--highlighted' : ''}`,
      grabbable: true,
      selectable: true,
      locked: false,
    };
  });

  const nodeData = new Map(nodeElements.map((element) => [
    Number((element.data as GraphElementNodeData).graphNodeId),
    element.data as GraphElementNodeData,
  ]));
  const remainingVisible = visibleEdgeCounts(visibleEdges);
  const indexedEdges = edges.map((edge, edgeIndex) => ({ edge, edgeIndex })).sort(compareIndexedEdges);
  const edgeElements: cytoscape.EdgeDefinition[] = [];
  const edgeIdentityOccurrences = new Map<string, number>();

  for (const { edge, edgeIndex } of indexedEdges) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) continue;
    const relationType = edge.relation_type || 'related';
    const identity = edgeIdentity(edge);
    const visibleCount = remainingVisible.get(identity) ?? 0;
    const visible = visibleCount > 0;
    if (visible) remainingVisible.set(identity, visibleCount - 1);
    const sourceData = nodeData.get(edge.source);
    const targetData = nodeData.get(edge.target);
    if (!sourceData || !targetData) continue;
    const weight = Math.max(0, finiteNumber(edge.weight, 1));
    const visualOccupancy = Math.max(0, (sourceData.visualWidth + targetData.visualWidth) / 2 - 44) * 0.42;
    const degreeClearance = Math.min(28, (Math.sqrt(sourceData.degree) + Math.sqrt(targetData.degree)) * 3);
    const weightTightening = Math.min(24, Math.log1p(weight) * 8);
    const idealLength = clamp(134 + visualOccupancy + degreeClearance - weightTightening, MIN_EDGE_LENGTH, MAX_EDGE_LENGTH);
    const elasticity = clamp(0.44 + Math.min(weight, 8) * 0.02, MIN_EDGE_ELASTICITY, MAX_EDGE_ELASTICITY);
    const relationClass = relationClasses.get(relationType) ?? 'graph-relation-0';
    const occurrence = edgeIdentityOccurrences.get(identity) ?? 0;
    edgeIdentityOccurrences.set(identity, occurrence + 1);
    const data: GraphElementEdgeData = {
      id: `edge-${stableStringHash(identity)}-${occurrence}`,
      source: String(edge.source),
      target: String(edge.target),
      edgeIndex,
      graphEdgeId: edge.id ?? null,
      relationType,
      relationClass,
      relationColor: colors.get(relationType) ?? GRAPH_RELATION_COLORS[0],
      memoryId: edge.memory_id ?? null,
      weight,
      idealLength,
      elasticity,
      visible: visible ? 1 : 0,
    };
    edgeElements.push({
      group: 'edges',
      data,
      classes: `graph-edge ${relationClass}${visible ? '' : ' graph-edge--hidden'}`,
      selectable: false,
    });
  }

  return {
    elements: [...nodeElements, ...edgeElements],
    nodeElements,
    edgeElements,
    relationColors: colors,
    dynamicEnabled: nodeElements.length <= MAX_DYNAMIC_GRAPH_NODES,
    dynamicDegraded: nodeElements.length > MAX_DYNAMIC_GRAPH_NODES,
  };
}

export function buildGraphElements(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
  relationColors?: ReadonlyMap<string, string>,
): cytoscape.ElementDefinition[] {
  return createGraphElementsModel(nodes, edges, edges, relationColors).elements;
}

export function buildGraphModel(
  nodes: readonly GraphNode[],
  allEdges: readonly GraphEdge[],
  visibleEdges: readonly GraphEdge[] = allEdges,
  relationColors?: ReadonlyMap<string, string>,
): GraphElementsModel {
  return createGraphElementsModel(nodes, allEdges, visibleEdges, relationColors);
}

export function createGraphStylesheet(colors: GraphStyleColors = {}): cytoscape.StylesheetJson {
  const nodeFill = colors.nodeFill ?? '#f5f4ef';
  const nodeStroke = colors.nodeStroke ?? '#69736b';
  const nodeText = colors.nodeText ?? '#20251f';
  const accent = colors.accent ?? '#68b65b';
  const focus = colors.focus ?? '#2f78c4';
  return [
    {
      selector: 'node',
      style: {
        width: 'data(visualWidth)',
        height: 'data(diameter)',
        label: 'data(label)',
        'font-size': 12,
        'font-weight': 700,
        color: nodeText,
        'background-color': nodeFill,
        'border-color': nodeStroke,
        'border-width': 3,
        'text-valign': 'center',
        'text-halign': 'center',
        'text-wrap': 'none',
      },
    },
    { selector: 'node.graph-node--highlighted', style: { 'background-color': accent, 'border-color': accent, 'border-width': 4 } },
    { selector: 'node:selected', style: { 'background-color': accent, 'border-color': focus, 'border-width': 5 } },
    { selector: 'edge', style: { width: 2.25, 'line-color': 'data(relationColor)', opacity: 0.32, 'curve-style': 'bezier' } },
    { selector: 'edge.graph-edge--hidden', style: { opacity: 0, 'events': 'no' } },
  ];
}

export function registerFcose(): void {
  if (fcoseRegistered) return;
  cytoscape.use(fcose);
  fcoseRegistered = true;
}

function safeNodeRepulsion(node: cytoscape.NodeSingular): number {
  return clamp(finiteNumber(node.data('repulsion'), MIN_NODE_REPULSION), MIN_NODE_REPULSION, MAX_NODE_REPULSION);
}

function safeIdealEdgeLength(edge: cytoscape.EdgeSingular): number {
  return clamp(finiteNumber(edge.data('idealLength'), MIN_EDGE_LENGTH), MIN_EDGE_LENGTH, MAX_EDGE_LENGTH);
}

function safeEdgeElasticity(edge: cytoscape.EdgeSingular): number {
  return clamp(finiteNumber(edge.data('elasticity'), 0.45), MIN_EDGE_ELASTICITY, MAX_EDGE_ELASTICITY);
}

function compareElementIds(leftId: string, rightId: string): number {
  const left = finiteNumber(leftId, Number.MAX_SAFE_INTEGER);
  const right = finiteNumber(rightId, Number.MAX_SAFE_INTEGER);
  return left - right || leftId.localeCompare(rightId);
}

interface VisualNodeBounds {
  halfWidth: number;
  halfHeight: number;
}

function visualNodeBounds(node: cytoscape.NodeSingular): VisualNodeBounds {
  const diameter = Math.max(0, finiteNumber(node.data('diameter'), MIN_NODE_RADIUS * 2));
  const visualWidth = Math.max(diameter, finiteNumber(node.data('visualWidth'), diameter));
  return { halfWidth: visualWidth / 2, halfHeight: diameter / 2 };
}

function countVisualOverlaps(
  nodes: readonly cytoscape.NodeSingular[],
  bounds: ReadonlyMap<string, VisualNodeBounds>,
  gap: number,
  tolerance: number,
): number {
  let overlaps = 0;
  for (let leftIndex = 0; leftIndex < nodes.length; leftIndex += 1) {
    const left = nodes[leftIndex];
    const leftBounds = bounds.get(left.id())!;
    const leftPosition = left.position();
    for (let rightIndex = leftIndex + 1; rightIndex < nodes.length; rightIndex += 1) {
      const right = nodes[rightIndex];
      const rightBounds = bounds.get(right.id())!;
      const rightPosition = right.position();
      const overlapX = leftBounds.halfWidth + rightBounds.halfWidth + gap - Math.abs(rightPosition.x - leftPosition.x);
      const overlapY = leftBounds.halfHeight + rightBounds.halfHeight + gap - Math.abs(rightPosition.y - leftPosition.y);
      if (overlapX > tolerance && overlapY > tolerance) overlaps += 1;
    }
  }
  return overlaps;
}

interface ProjectionInterval {
  min: number;
  max: number;
}

function nearestOutsideIntervals(value: number, intervals: readonly ProjectionInterval[]): number {
  if (intervals.length === 0) return value;
  const sorted = [...intervals].sort((left, right) => left.min - right.min || left.max - right.max);
  const merged: ProjectionInterval[] = [];
  for (const interval of sorted) {
    const previous = merged[merged.length - 1];
    if (!previous || interval.min > previous.max) merged.push({ ...interval });
    else previous.max = Math.max(previous.max, interval.max);
  }
  for (const interval of merged) {
    if (value < interval.min || value > interval.max) continue;
    const negativeDistance = value - interval.min;
    const positiveDistance = interval.max - value;
    return negativeDistance < positiveDistance ? interval.min : interval.max;
  }
  return value;
}

function packRemainingVisualOverlaps(
  nodes: readonly cytoscape.NodeSingular[],
  bounds: ReadonlyMap<string, VisualNodeBounds>,
  gap: number,
  tolerance: number,
): { movedNodeIds: Set<string>; totalDisplacement: number } {
  const movedNodeIds = new Set<string>();
  let totalDisplacement = 0;
  const placed = nodes.filter((node) => node.locked());
  const movable = nodes.filter((node) => !node.locked());

  for (const node of movable) {
    const nodeBounds = bounds.get(node.id())!;
    const position = node.position();
    const horizontalIntervals: ProjectionInterval[] = [];
    const verticalIntervals: ProjectionInterval[] = [];
    for (const obstacle of placed) {
      const obstacleBounds = bounds.get(obstacle.id())!;
      const obstaclePosition = obstacle.position();
      const requiredX = nodeBounds.halfWidth + obstacleBounds.halfWidth + gap + tolerance;
      const requiredY = nodeBounds.halfHeight + obstacleBounds.halfHeight + gap + tolerance;
      if (Math.abs(position.y - obstaclePosition.y) < requiredY) {
        horizontalIntervals.push({ min: obstaclePosition.x - requiredX, max: obstaclePosition.x + requiredX });
      }
      if (Math.abs(position.x - obstaclePosition.x) < requiredX) {
        verticalIntervals.push({ min: obstaclePosition.y - requiredY, max: obstaclePosition.y + requiredY });
      }
    }

    const horizontalX = nearestOutsideIntervals(position.x, horizontalIntervals);
    const verticalY = nearestOutsideIntervals(position.y, verticalIntervals);
    const horizontalDisplacement = Math.abs(horizontalX - position.x);
    const verticalDisplacement = Math.abs(verticalY - position.y);
    const nextPosition = horizontalDisplacement <= verticalDisplacement
      ? { x: horizontalX, y: position.y }
      : { x: position.x, y: verticalY };
    const displacement = Math.hypot(nextPosition.x - position.x, nextPosition.y - position.y);
    if (displacement > 0) {
      node.position(nextPosition);
      movedNodeIds.add(node.id());
      totalDisplacement += displacement;
    }
    placed.push(node);
  }

  return { movedNodeIds, totalDisplacement };
}

export function resolveVisualOverlaps(
  cy: cytoscape.Core,
  options: VisualOverlapOptions = {},
): VisualOverlapResult {
  const gap = clamp(finiteNumber(options.gap, 14), 0, 160);
  const maxIterations = Math.round(clamp(finiteNumber(options.maxIterations, 12), 1, 50));
  const tolerance = clamp(finiteNumber(options.tolerance, 0.01), 0, 1);
  const nodes: cytoscape.NodeSingular[] = [];
  cy.nodes().forEach((node) => {
    nodes.push(node);
  });
  nodes.sort((left, right) => compareElementIds(left.id(), right.id()));
  const bounds = new Map(nodes.map((node) => [node.id(), visualNodeBounds(node)]));
  const movedNodeIds = new Set<string>();
  let totalDisplacement = 0;
  let iterations = 0;
  const initialOverlaps = countVisualOverlaps(nodes, bounds, gap, tolerance);
  let remainingOverlaps = initialOverlaps;

  for (let iteration = 0; iteration < maxIterations && remainingOverlaps > 0; iteration += 1) {
    let movedThisIteration = false;
    cy.batch(() => {
      for (let leftIndex = 0; leftIndex < nodes.length; leftIndex += 1) {
        const left = nodes[leftIndex];
        const leftBounds = bounds.get(left.id())!;
        for (let rightIndex = leftIndex + 1; rightIndex < nodes.length; rightIndex += 1) {
          const right = nodes[rightIndex];
          const rightBounds = bounds.get(right.id())!;
          const leftPosition = left.position();
          const rightPosition = right.position();
          const deltaX = rightPosition.x - leftPosition.x;
          const deltaY = rightPosition.y - leftPosition.y;
          const overlapX = leftBounds.halfWidth + rightBounds.halfWidth + gap - Math.abs(deltaX);
          const overlapY = leftBounds.halfHeight + rightBounds.halfHeight + gap - Math.abs(deltaY);
          if (overlapX <= tolerance || overlapY <= tolerance) continue;

          const leftLocked = left.locked();
          const rightLocked = right.locked();
          if (leftLocked && rightLocked) continue;

          const separateOnX = overlapX <= overlapY;
          const axis = separateOnX ? 'x' : 'y';
          const delta = separateOnX ? deltaX : deltaY;
          const direction = delta < -tolerance ? -1 : 1;
          const displacement = (separateOnX ? overlapX : overlapY) + tolerance;
          const leftShare = leftLocked ? 0 : rightLocked ? displacement : displacement / 2;
          const rightShare = rightLocked ? 0 : leftLocked ? displacement : displacement / 2;

          if (leftShare > 0) {
            left.position(axis, leftPosition[axis] - direction * leftShare);
            movedNodeIds.add(left.id());
            totalDisplacement += leftShare;
          }
          if (rightShare > 0) {
            right.position(axis, rightPosition[axis] + direction * rightShare);
            movedNodeIds.add(right.id());
            totalDisplacement += rightShare;
          }
          movedThisIteration = true;
        }
      }
    });
    iterations = iteration + 1;
    remainingOverlaps = countVisualOverlaps(nodes, bounds, gap, tolerance);
    if (!movedThisIteration) break;
  }

  if (remainingOverlaps > 0) {
    const packed = packRemainingVisualOverlaps(nodes, bounds, gap, tolerance);
    packed.movedNodeIds.forEach((id) => movedNodeIds.add(id));
    totalDisplacement += packed.totalDisplacement;
    remainingOverlaps = countVisualOverlaps(nodes, bounds, gap, tolerance);
  }

  return {
    iterations,
    initialOverlaps,
    remainingOverlaps,
    movedNodeCount: movedNodeIds.size,
    totalDisplacement,
  };
}

export function createFcoseLayoutOptions(
  elements: readonly cytoscape.ElementDefinition[],
  options: GraphLayoutOptions = {},
): FcoseLayoutOptions {
  const nodeCount = elements.reduce((count, element) => count + (element.group === 'nodes' ? 1 : 0), 0);
  return {
    name: 'fcose',
    // visualWidth already includes label occupancy in each node's dimensions. "default" is the
    // <=80-node main-thread budget tradeoff; proof measured ~323ms/472ms at 48/80 nodes,
    // while the default pair completed in about 311ms combined.
    quality: 'default',
    randomize: false,
    animate: options.animate ?? false,
    fit: options.fit ?? true,
    padding: clamp(finiteNumber(options.padding, GRAPH_LAYOUT_PADDING), 0, 160),
    nodeDimensionsIncludeLabels: true,
    uniformNodeDimensions: false,
    packComponents: true,
    step: 'all',
    samplingType: true,
    sampleSize: clamp(nodeCount, 10, 25),
    nodeSeparation: 75,
    piTol: 0.0000001,
    nodeRepulsion: safeNodeRepulsion,
    idealEdgeLength: safeIdealEdgeLength,
    edgeElasticity: safeEdgeElasticity,
    nestingFactor: 0.1,
    numIter: nodeCount >= 64 ? 650 : 500,
    tile: true,
    tilingCompareBy: compareElementIds,
    tilingPaddingVertical: 18,
    tilingPaddingHorizontal: 18,
    gravity: 0.2,
    gravityRangeCompound: 1.5,
    gravityCompound: 1,
    gravityRange: 3.8,
    initialEnergyOnIncremental: 0.2,
  };
}

export function createFcoseLayout(
  cy: cytoscape.Core,
  elements: readonly cytoscape.ElementDefinition[],
  options: GraphLayoutOptions = {},
  layoutElements: cytoscape.CollectionReturnValue = cy.elements(),
): cytoscape.Layouts {
  registerFcose();
  return layoutElements.layout(createFcoseLayoutOptions(elements, options) as cytoscape.LayoutOptions);
}

export function createHeadlessGraph(elements: readonly cytoscape.ElementDefinition[]): cytoscape.Core {
  registerFcose();
  return cytoscape({
    headless: true,
    styleEnabled: true,
    elements: [...elements],
    style: createGraphStylesheet(),
    layout: { name: 'preset', fit: false },
    minZoom: 0.65,
    maxZoom: 2.5,
  });
}

function safeNodePosition(node: cytoscape.NodeSingular, position: cytoscape.Position): cytoscape.Position {
  const current = node.position();
  return {
    x: Number.isFinite(position.x) ? position.x : finiteNumber(current.x),
    y: Number.isFinite(position.y) ? position.y : finiteNumber(current.y),
  };
}

export function lockGraphNode(
  cy: cytoscape.Core,
  graphNodeId: number,
  position?: cytoscape.Position,
): cytoscape.NodeSingular | undefined {
  const node = cy.getElementById(String(graphNodeId));
  if (!node.isNode()) return undefined;
  node.unlock();
  if (position) node.position(safeNodePosition(node, position));
  node.lock();
  return node;
}

export function moveLockedGraphNode(
  cy: cytoscape.Core,
  graphNodeId: number,
  position: cytoscape.Position,
): cytoscape.NodeSingular | undefined {
  const node = cy.getElementById(String(graphNodeId));
  if (!node.isNode()) return undefined;
  node.unlock();
  node.position(safeNodePosition(node, position));
  node.lock();
  return node;
}

export function releaseGraphNode(cy: cytoscape.Core, graphNodeId: number): cytoscape.NodeSingular | undefined {
  const node = cy.getElementById(String(graphNodeId));
  if (!node.isNode()) return undefined;
  node.unlock();
  return node;
}

export type ManualLockAction =
  | { type: 'lock'; nodeId: number }
  | { type: 'reflow' };

export function updateManualLocks(
  current: ReadonlySet<number>,
  action: ManualLockAction,
): ReadonlySet<number> {
  if (action.type === 'reflow') return new Set<number>();
  const next = new Set(current);
  next.add(action.nodeId);
  return next;
}

export interface VisibleGraphTopology {
  nodeIds: readonly number[];
  edgeIndexes: ReadonlySet<number>;
}

export function visibleEdgeIndexes(allEdges: readonly GraphEdge[], visibleEdges: readonly GraphEdge[]): ReadonlySet<number> {
  const referenceSet = new Set(visibleEdges);
  const remainingByIdentity = visibleEdgeCounts(visibleEdges);
  const indexes = new Set<number>();
  allEdges.forEach((edge, edgeIndex) => {
    if (referenceSet.has(edge)) {
      indexes.add(edgeIndex);
      const identity = edgeIdentity(edge);
      remainingByIdentity.set(identity, Math.max(0, (remainingByIdentity.get(identity) ?? 0) - 1));
      return;
    }
    const identity = edgeIdentity(edge);
    const remaining = remainingByIdentity.get(identity) ?? 0;
    if (remaining > 0) {
      indexes.add(edgeIndex);
      remainingByIdentity.set(identity, remaining - 1);
    }
  });
  return indexes;
}

export function buildVisibleTopology(
  nodes: readonly GraphNode[],
  allEdges: readonly GraphEdge[],
  visibleEdges: readonly GraphEdge[],
): VisibleGraphTopology {
  const nodeIds = [...new Set(nodes.map((node) => node.id))].sort((left, right) => left - right);
  const nodeIdSet = new Set(nodeIds);
  const candidateIndexes = visibleEdgeIndexes(allEdges, visibleEdges);
  const edgeIndexes = new Set<number>();
  for (const edgeIndex of candidateIndexes) {
    const edge = allEdges[edgeIndex];
    if (edge && nodeIdSet.has(edge.source) && nodeIdSet.has(edge.target)) edgeIndexes.add(edgeIndex);
  }
  return { nodeIds, edgeIndexes };
}

export function setVisibleEdges(
  cy: cytoscape.Core,
  allEdges: readonly GraphEdge[],
  visibleEdges: readonly GraphEdge[],
): void {
  const visibleIndexes = visibleEdgeIndexes(allEdges, visibleEdges);
  cy.batch(() => {
    cy.edges().forEach((edge) => {
      const visible = visibleIndexes.has(finiteNumber(edge.data('edgeIndex'), -1));
      edge.data('visible', visible ? 1 : 0);
      if (visible) edge.removeClass('graph-edge--hidden');
      else edge.addClass('graph-edge--hidden');
    });
  });
}

function seedPositions(elements: readonly cytoscape.ElementDefinition[]): ReadonlyMap<string, cytoscape.Position> {
  const positions = new Map<string, cytoscape.Position>();
  for (const element of elements) {
    if (element.group !== 'nodes' || !element.position) continue;
    const id = String(element.data.id);
    positions.set(id, { x: element.position.x, y: element.position.y });
  }
  return positions;
}

export function reflowGraph(
  cy: cytoscape.Core,
  elements: readonly cytoscape.ElementDefinition[],
  options: GraphLayoutOptions & { runLayout?: boolean; layoutElements?: cytoscape.CollectionReturnValue } = {},
): cytoscape.Layouts | undefined {
  cy.stop(true, false);
  const positions = seedPositions(elements);
  cy.batch(() => {
    cy.nodes().forEach((node) => {
      node.unlock();
      const position = positions.get(node.id());
      if (position) node.position(position);
    });
  });
  if (options.runLayout === false || cy.nodes().length > MAX_DYNAMIC_GRAPH_NODES) return undefined;
  const layout = createFcoseLayout(cy, elements, options, options.layoutElements ?? cy.elements());
  layout.run();
  return layout;
}
