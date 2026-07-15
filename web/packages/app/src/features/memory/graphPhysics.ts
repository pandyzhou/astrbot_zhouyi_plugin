import type { GraphEdge, GraphNode } from './types';

export const DEFAULT_GRAPH_WIDTH = 1000;
export const DEFAULT_GRAPH_HEIGHT = 640;
export const MAX_DYNAMIC_GRAPH_NODES = 80;
export const MIN_COLLISION_RADIUS = 22;
export const COLLISION_GAP = 8;
export const SPRING_PADDING = 70;
export const MIN_GRAPH_ZOOM = 0.65;
export const MAX_GRAPH_ZOOM = 2.5;
export const MAX_GRAPH_PAN_X = 360;
export const MAX_GRAPH_PAN_Y = 240;

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
const REPULSION_STRENGTH = 1050;
const SPRING_STRENGTH = 0.018;
const CENTER_STRENGTH = 0.0018;
const VELOCITY_DAMPING = 0.85;
const MAX_SPEED = 11;
const ALPHA_DECAY = 0.985;
const MIN_ALPHA = 0.002;
const STABLE_SPEED = 0.08;
const STABLE_OVERLAP = 0.2;
const STABLE_FRAMES_REQUIRED = 18;
const DEFAULT_MAX_FRAMES = 900;

export interface PhysicsNode {
  node: GraphNode;
  x: number;
  y: number;
  vx: number;
  vy: number;
  radius: number;
  fixedX?: number;
  fixedY?: number;
}

export interface PhysicsEdge {
  edge: GraphEdge;
  source: PhysicsNode;
  target: PhysicsNode;
  sourceId: number;
  targetId: number;
  restLength: number;
}

export interface GraphSimulation {
  nodes: PhysicsNode[];
  edges: PhysicsEdge[];
  width: number;
  height: number;
  dynamicEnabled: boolean;
  dynamicDegraded: boolean;
  alpha: number;
  frame: number;
  maxFrames: number;
  stableFrames: number;
  lastMaxSpeed: number;
  lastOverlapCount: number;
  lastMaxOverlap: number;
}

export interface GraphSimulationStep {
  frame: number;
  alpha: number;
  kineticEnergy: number;
  maxSpeed: number;
  overlapCount: number;
  maxOverlap: number;
  shouldContinue: boolean;
}

export interface GraphPoint {
  x: number;
  y: number;
}

export interface GraphView {
  zoom: number;
  pan: GraphPoint;
}

export type GraphZoomChange = { wheelDelta: number } | { nextZoom: number };

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function finiteNumber(value: unknown): number {
  const number = Number(value ?? 0);
  return Number.isFinite(number) ? number : 0;
}

function compareGraphNodes(left: GraphNode, right: GraphNode): number {
  return (
    finiteNumber(right.weight) - finiteNumber(left.weight) ||
    finiteNumber(right.degree) - finiteNumber(left.degree) ||
    left.id - right.id
  );
}

function collisionRadius(node: PhysicsNode): number {
  return Math.max(node.radius, MIN_COLLISION_RADIUS);
}

function nodeBounds(node: PhysicsNode, width: number, height: number): { minX: number; maxX: number; minY: number; maxY: number } {
  const radius = collisionRadius(node);
  return {
    minX: Math.min(radius, width / 2),
    maxX: Math.max(width - radius, width / 2),
    minY: Math.min(radius, height / 2),
    maxY: Math.max(height - radius, height / 2),
  };
}

function clampNodePosition(node: PhysicsNode, x: number, y: number, width: number, height: number): GraphPoint {
  const bounds = nodeBounds(node, width, height);
  return {
    x: clamp(x, bounds.minX, bounds.maxX),
    y: clamp(y, bounds.minY, bounds.maxY),
  };
}

function createInitialLayout(nodes: GraphNode[], width: number, height: number): PhysicsNode[] {
  const ranked = [...nodes].sort(compareGraphNodes);
  const maxWeight = Math.max(...ranked.map((node) => Math.max(0, finiteNumber(node.weight))), 1);
  const horizontalRadius = width * 0.405;
  const verticalRadius = height * 0.390625;

  return ranked.map((node, index) => {
    const progress = Math.sqrt((index + 0.65) / Math.max(ranked.length, 1));
    const angle = index * GOLDEN_ANGLE;
    const radius = 16 + 8 * Math.sqrt(Math.max(0, finiteNumber(node.weight)) / maxWeight);
    const physicsNode: PhysicsNode = {
      node,
      x: width / 2 + Math.cos(angle) * horizontalRadius * progress,
      y: height / 2 + Math.sin(angle) * verticalRadius * progress,
      vx: 0,
      vy: 0,
      radius,
    };
    const position = clampNodePosition(physicsNode, physicsNode.x, physicsNode.y, width, height);
    physicsNode.x = position.x;
    physicsNode.y = position.y;
    return physicsNode;
  });
}

function compareGraphEdges(left: GraphEdge, right: GraphEdge): number {
  return left.source - right.source || left.target - right.target || finiteNumber(left.id) - finiteNumber(right.id);
}

export function createGraphSimulation(
  nodes: GraphNode[],
  edges: GraphEdge[],
  width = DEFAULT_GRAPH_WIDTH,
  height = DEFAULT_GRAPH_HEIGHT,
): GraphSimulation {
  const safeWidth = Math.max(1, finiteNumber(width));
  const safeHeight = Math.max(1, finiteNumber(height));
  const physicsNodes = createInitialLayout(nodes, safeWidth, safeHeight);
  const nodeById = new Map(physicsNodes.map((node) => [node.node.id, node]));
  const physicsEdges = [...edges]
    .sort(compareGraphEdges)
    .flatMap((edge): PhysicsEdge[] => {
      const source = nodeById.get(edge.source);
      const target = nodeById.get(edge.target);
      if (!source || !target) return [];
      return [{
        edge,
        source,
        target,
        sourceId: source.node.id,
        targetId: target.node.id,
        restLength: collisionRadius(source) + collisionRadius(target) + SPRING_PADDING,
      }];
    });
  const dynamicEnabled = physicsNodes.length <= MAX_DYNAMIC_GRAPH_NODES;

  return {
    nodes: physicsNodes,
    edges: physicsEdges,
    width: safeWidth,
    height: safeHeight,
    dynamicEnabled,
    dynamicDegraded: !dynamicEnabled,
    alpha: dynamicEnabled ? 1 : 0,
    frame: 0,
    maxFrames: DEFAULT_MAX_FRAMES,
    stableFrames: 0,
    lastMaxSpeed: 0,
    lastOverlapCount: 0,
    lastMaxOverlap: 0,
  };
}

function stableDirection(leftId: number, rightId: number): GraphPoint {
  const low = Math.min(leftId, rightId);
  const high = Math.max(leftId, rightId);
  let hash = 2166136261;
  const key = `${low}:${high}`;
  for (let index = 0; index < key.length; index += 1) {
    hash ^= key.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  const angle = ((hash >>> 0) / 0xffffffff) * Math.PI * 2;
  const direction = { x: Math.cos(angle), y: Math.sin(angle) };
  return leftId <= rightId ? direction : { x: -direction.x, y: -direction.y };
}

function directionBetween(left: PhysicsNode, right: PhysicsNode): { x: number; y: number; distance: number } {
  const dx = right.x - left.x;
  const dy = right.y - left.y;
  const distance = Math.hypot(dx, dy);
  if (distance > 1e-9) return { x: dx / distance, y: dy / distance, distance };
  const direction = stableDirection(left.node.id, right.node.id);
  return { ...direction, distance: 0 };
}

function isFixed(node: PhysicsNode): boolean {
  return node.fixedX !== undefined && node.fixedY !== undefined;
}

function applyPairVelocity(left: PhysicsNode, right: PhysicsNode, x: number, y: number): void {
  if (!isFixed(left)) {
    left.vx -= x;
    left.vy -= y;
  }
  if (!isFixed(right)) {
    right.vx += x;
    right.vy += y;
  }
}

function separateCollision(left: PhysicsNode, right: PhysicsNode, unitX: number, unitY: number, overlap: number, width: number, height: number): void {
  const leftFixed = isFixed(left);
  const rightFixed = isFixed(right);
  if (leftFixed && rightFixed) return;

  const leftShare = rightFixed ? overlap : overlap / 2;
  const rightShare = leftFixed ? overlap : overlap / 2;
  if (!leftFixed) {
    const position = clampNodePosition(left, left.x - unitX * leftShare, left.y - unitY * leftShare, width, height);
    left.x = position.x;
    left.y = position.y;
  }
  if (!rightFixed) {
    const position = clampNodePosition(right, right.x + unitX * rightShare, right.y + unitY * rightShare, width, height);
    right.x = position.x;
    right.y = position.y;
  }
}

export function stepGraphSimulation(simulation: GraphSimulation, timeScale = 1): GraphSimulationStep {
  const dt = clamp(finiteNumber(timeScale), 0.05, 3);
  if (!simulation.dynamicEnabled) {
    return {
      frame: simulation.frame,
      alpha: simulation.alpha,
      kineticEnergy: 0,
      maxSpeed: 0,
      overlapCount: 0,
      maxOverlap: 0,
      shouldContinue: false,
    };
  }

  const forceScale = simulation.alpha * dt;
  let overlapCount = 0;
  let maxOverlap = 0;

  for (let leftIndex = 0; leftIndex < simulation.nodes.length; leftIndex += 1) {
    const left = simulation.nodes[leftIndex];
    for (let rightIndex = leftIndex + 1; rightIndex < simulation.nodes.length; rightIndex += 1) {
      const right = simulation.nodes[rightIndex];
      const direction = directionBetween(left, right);
      const effectiveDistance = Math.max(direction.distance, 1);
      const repulsion = Math.min(2.5, (REPULSION_STRENGTH / (effectiveDistance * effectiveDistance)) * forceScale);
      applyPairVelocity(left, right, direction.x * repulsion, direction.y * repulsion);

      const minimumDistance = collisionRadius(left) + collisionRadius(right) + COLLISION_GAP;
      const overlap = minimumDistance - direction.distance;
      if (overlap > 0) {
        overlapCount += 1;
        maxOverlap = Math.max(maxOverlap, overlap);
        separateCollision(left, right, direction.x, direction.y, overlap, simulation.width, simulation.height);
        applyPairVelocity(left, right, direction.x * Math.min(overlap * 0.035, 1.5), direction.y * Math.min(overlap * 0.035, 1.5));
      }
    }
  }

  for (const edge of simulation.edges) {
    const direction = directionBetween(edge.source, edge.target);
    const stretch = direction.distance - edge.restLength;
    const spring = clamp(stretch * SPRING_STRENGTH * forceScale, -2.5, 2.5);
    applyPairVelocity(edge.source, edge.target, -direction.x * spring, -direction.y * spring);
  }

  const centerX = simulation.width / 2;
  const centerY = simulation.height / 2;
  const damping = Math.pow(VELOCITY_DAMPING, dt);
  let kineticEnergy = 0;
  let maxSpeed = 0;

  for (const node of simulation.nodes) {
    if (isFixed(node)) {
      node.x = node.fixedX as number;
      node.y = node.fixedY as number;
      node.vx = 0;
      node.vy = 0;
      continue;
    }

    node.vx += (centerX - node.x) * CENTER_STRENGTH * forceScale;
    node.vy += (centerY - node.y) * CENTER_STRENGTH * forceScale;
    node.vx *= damping;
    node.vy *= damping;

    const speed = Math.hypot(node.vx, node.vy);
    if (speed > MAX_SPEED) {
      const speedScale = MAX_SPEED / speed;
      node.vx *= speedScale;
      node.vy *= speedScale;
    }

    node.x += node.vx * dt;
    node.y += node.vy * dt;
    const position = clampNodePosition(node, node.x, node.y, simulation.width, simulation.height);
    if (position.x !== node.x) node.vx *= -0.2;
    if (position.y !== node.y) node.vy *= -0.2;
    node.x = position.x;
    node.y = position.y;

    const finalSpeed = Math.hypot(node.vx, node.vy);
    kineticEnergy += finalSpeed * finalSpeed;
    maxSpeed = Math.max(maxSpeed, finalSpeed);
  }

  simulation.frame += 1;
  simulation.alpha = Math.max(0, simulation.alpha * Math.pow(ALPHA_DECAY, dt));
  simulation.lastMaxSpeed = maxSpeed;
  simulation.lastOverlapCount = overlapCount;
  simulation.lastMaxOverlap = maxOverlap;
  if (maxSpeed <= STABLE_SPEED && maxOverlap <= STABLE_OVERLAP && simulation.alpha <= 0.03) {
    simulation.stableFrames += 1;
  } else {
    simulation.stableFrames = 0;
  }

  return {
    frame: simulation.frame,
    alpha: simulation.alpha,
    kineticEnergy,
    maxSpeed,
    overlapCount,
    maxOverlap,
    shouldContinue: shouldContinueGraphSimulation(simulation),
  };
}

export function shouldContinueGraphSimulation(simulation: GraphSimulation): boolean {
  if (!simulation.dynamicEnabled || simulation.frame >= simulation.maxFrames) return false;
  if (simulation.stableFrames >= STABLE_FRAMES_REQUIRED) return false;
  return simulation.alpha > MIN_ALPHA || simulation.lastMaxSpeed > STABLE_SPEED || simulation.lastMaxOverlap > STABLE_OVERLAP;
}

export function settleGraphSimulation(simulation: GraphSimulation, maxSteps = 600): GraphSimulationStep {
  let result: GraphSimulationStep = {
    frame: simulation.frame,
    alpha: simulation.alpha,
    kineticEnergy: 0,
    maxSpeed: simulation.lastMaxSpeed,
    overlapCount: simulation.lastOverlapCount,
    maxOverlap: simulation.lastMaxOverlap,
    shouldContinue: shouldContinueGraphSimulation(simulation),
  };
  const steps = Math.max(0, Math.floor(finiteNumber(maxSteps)));
  for (let index = 0; index < steps && result.shouldContinue; index += 1) {
    result = stepGraphSimulation(simulation);
  }
  return result;
}

export function reheatGraphSimulation(simulation: GraphSimulation, alpha = 1): GraphSimulation {
  if (!simulation.dynamicEnabled) return simulation;
  simulation.alpha = clamp(finiteNumber(alpha), MIN_ALPHA, 1);
  simulation.frame = 0;
  simulation.stableFrames = 0;
  return simulation;
}

export function resetGraphSimulation(simulation: GraphSimulation): GraphSimulation {
  const initialNodes = createInitialLayout(simulation.nodes.map((item) => item.node), simulation.width, simulation.height);
  const initialById = new Map(initialNodes.map((node) => [node.node.id, node]));
  simulation.nodes.sort((left, right) => compareGraphNodes(left.node, right.node));
  for (const node of simulation.nodes) {
    const initial = initialById.get(node.node.id);
    if (!initial) continue;
    node.x = initial.x;
    node.y = initial.y;
    node.vx = 0;
    node.vy = 0;
    node.radius = initial.radius;
    delete node.fixedX;
    delete node.fixedY;
  }
  simulation.lastMaxSpeed = 0;
  simulation.lastOverlapCount = 0;
  simulation.lastMaxOverlap = 0;
  simulation.alpha = simulation.dynamicEnabled ? 1 : 0;
  simulation.frame = 0;
  simulation.stableFrames = 0;
  return simulation;
}

export const reflowGraphSimulation = resetGraphSimulation;

function getPhysicsNode(simulation: GraphSimulation, nodeId: number): PhysicsNode | undefined {
  return simulation.nodes.find((node) => node.node.id === nodeId);
}

export function pinGraphNode(simulation: GraphSimulation, nodeId: number, x?: number, y?: number): PhysicsNode | undefined {
  const node = getPhysicsNode(simulation, nodeId);
  if (!node) return undefined;
  const position = clampNodePosition(node, x ?? node.x, y ?? node.y, simulation.width, simulation.height);
  node.fixedX = position.x;
  node.fixedY = position.y;
  node.x = position.x;
  node.y = position.y;
  node.vx = 0;
  node.vy = 0;
  reheatGraphSimulation(simulation, 0.7);
  return node;
}

export function moveGraphNode(simulation: GraphSimulation, nodeId: number, x: number, y: number): PhysicsNode | undefined {
  const node = getPhysicsNode(simulation, nodeId);
  if (!node) return undefined;
  const position = clampNodePosition(node, x, y, simulation.width, simulation.height);
  node.fixedX = position.x;
  node.fixedY = position.y;
  node.x = position.x;
  node.y = position.y;
  node.vx = 0;
  node.vy = 0;
  reheatGraphSimulation(simulation, 0.7);
  return node;
}

export function releaseGraphNode(simulation: GraphSimulation, nodeId: number): PhysicsNode | undefined {
  const node = getPhysicsNode(simulation, nodeId);
  if (!node) return undefined;
  delete node.fixedX;
  delete node.fixedY;
  reheatGraphSimulation(simulation, 0.85);
  return node;
}

export function normalizeWheelDelta(deltaY: number, deltaMode: number, pageSize: number): number {
  const delta = finiteNumber(deltaY);
  if (deltaMode === 1) return delta * 16;
  if (deltaMode === 2) return delta * Math.max(1, finiteNumber(pageSize));
  return delta;
}

export function clampGraphPan(pan: GraphPoint): GraphPoint {
  return {
    x: clamp(finiteNumber(pan.x), -MAX_GRAPH_PAN_X, MAX_GRAPH_PAN_X),
    y: clamp(finiteNumber(pan.y), -MAX_GRAPH_PAN_Y, MAX_GRAPH_PAN_Y),
  };
}

export function zoomAtPoint(
  view: GraphView,
  anchor: GraphPoint,
  change: GraphZoomChange,
  width = DEFAULT_GRAPH_WIDTH,
  height = DEFAULT_GRAPH_HEIGHT,
): GraphView {
  const currentZoom = clamp(finiteNumber(view.zoom) || 1, MIN_GRAPH_ZOOM, MAX_GRAPH_ZOOM);
  const requestedZoom = 'nextZoom' in change
    ? finiteNumber(change.nextZoom)
    : currentZoom * Math.exp(-finiteNumber(change.wheelDelta) * 0.0015);
  const nextZoom = clamp(requestedZoom, MIN_GRAPH_ZOOM, MAX_GRAPH_ZOOM);
  const centerX = finiteNumber(width) / 2;
  const centerY = finiteNumber(height) / 2;
  const ratio = nextZoom / currentZoom;
  const nextPan = clampGraphPan({
    x: anchor.x - centerX - ratio * (anchor.x - centerX - view.pan.x),
    y: anchor.y - centerY - ratio * (anchor.y - centerY - view.pan.y),
  });
  return { zoom: nextZoom, pan: nextPan };
}
