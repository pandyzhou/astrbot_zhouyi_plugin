import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { GraphEdge, GraphNode } from './types';
import {
  COLLISION_GAP,
  MAX_DYNAMIC_GRAPH_NODES,
  MAX_GRAPH_ZOOM,
  MIN_COLLISION_RADIUS,
  MIN_GRAPH_ZOOM,
  clampGraphPan,
  createGraphSimulation,
  moveGraphNode,
  normalizeWheelDelta,
  pinGraphNode,
  releaseGraphNode,
  reflowGraphSimulation,
  settleGraphSimulation,
  shouldContinueGraphSimulation,
  stepGraphSimulation,
  zoomAtPoint,
} from './graphPhysics';

function graphNode(id: number, weight = 1, degree = 0): GraphNode {
  return { id, label: `node-${id}`, weight, degree };
}

function graphEdge(source: number, target: number, id = 1): GraphEdge {
  return { id, source, target };
}

function byId(simulation: ReturnType<typeof createGraphSimulation>, id: number) {
  const node = simulation.nodes.find((item) => item.node.id === id);
  assert.ok(node, `missing node ${id}`);
  return node;
}

function distance(left: { x: number; y: number }, right: { x: number; y: number }): number {
  return Math.hypot(right.x - left.x, right.y - left.y);
}

function assertClose(actual: number, expected: number, tolerance = 1e-8): void {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} is not within ${tolerance} of ${expected}`);
}

function graphToScreen(
  point: { x: number; y: number },
  view: { zoom: number; pan: { x: number; y: number } },
  width = 1000,
  height = 640,
) {
  return {
    x: width / 2 + view.pan.x + view.zoom * (point.x - width / 2),
    y: height / 2 + view.pan.y + view.zoom * (point.y - height / 2),
  };
}

function screenToGraph(
  point: { x: number; y: number },
  view: { zoom: number; pan: { x: number; y: number } },
  width = 1000,
  height = 640,
) {
  return {
    x: width / 2 + (point.x - width / 2 - view.pan.x) / view.zoom,
    y: height / 2 + (point.y - height / 2 - view.pan.y) / view.zoom,
  };
}

test('initial layout is deterministic across input order', () => {
  const nodes = [graphNode(9, 2, 1), graphNode(2, 5, 3), graphNode(6, 5, 1), graphNode(4, 0, 8)];
  const forward = createGraphSimulation(nodes, []);
  const reversed = createGraphSimulation([...nodes].reverse(), []);

  for (const node of nodes) {
    const left = byId(forward, node.id);
    const right = byId(reversed, node.id);
    assert.equal(left.x, right.x);
    assert.equal(left.y, right.y);
    assert.equal(left.radius, right.radius);
  }
});

test('equal weight and degree use id as the stable ranking tie-breaker', () => {
  const simulation = createGraphSimulation([graphNode(8, 2, 3), graphNode(3, 2, 3), graphNode(5, 2, 3)], []);
  assert.deepEqual(simulation.nodes.map((item) => item.node.id), [3, 5, 8]);
});

test('missing edge endpoints are filtered', () => {
  const simulation = createGraphSimulation(
    [graphNode(1), graphNode(2)],
    [graphEdge(1, 2, 1), graphEdge(1, 99, 2), graphEdge(98, 2, 3)],
  );
  assert.equal(simulation.edges.length, 1);
  assert.equal(simulation.edges[0].sourceId, 1);
  assert.equal(simulation.edges[0].targetId, 2);
});

test('coincident nodes remain finite and separate deterministically', () => {
  const simulation = createGraphSimulation([graphNode(1), graphNode(2)], []);
  for (const node of simulation.nodes) {
    node.x = 500;
    node.y = 320;
  }

  for (let index = 0; index < 20; index += 1) stepGraphSimulation(simulation);

  for (const node of simulation.nodes) {
    assert.ok(Number.isFinite(node.x));
    assert.ok(Number.isFinite(node.y));
    assert.ok(Number.isFinite(node.vx));
    assert.ok(Number.isFinite(node.vy));
  }
  assert.ok(distance(simulation.nodes[0], simulation.nodes[1]) > 0);
});

test('overlapping nodes reach the minimum collision distance in finite steps', () => {
  const simulation = createGraphSimulation([graphNode(1), graphNode(2)], []);
  const left = byId(simulation, 1);
  const right = byId(simulation, 2);
  left.x = 500;
  left.y = 320;
  right.x = 500;
  right.y = 320;

  settleGraphSimulation(simulation, 240);

  const minimumDistance = Math.max(left.radius, MIN_COLLISION_RADIUS) + Math.max(right.radius, MIN_COLLISION_RADIUS) + COLLISION_GAP;
  assert.ok(distance(left, right) >= minimumDistance - 0.75);
});

test('spring-connected nodes move toward rest length without diverging', () => {
  const simulation = createGraphSimulation([graphNode(1), graphNode(2)], [graphEdge(1, 2)]);
  const left = byId(simulation, 1);
  const right = byId(simulation, 2);
  left.x = 100;
  left.y = 320;
  right.x = 900;
  right.y = 320;
  left.vx = 0;
  left.vy = 0;
  right.vx = 0;
  right.vy = 0;
  const initialDistance = distance(left, right);
  const restLength = simulation.edges[0].restLength;

  settleGraphSimulation(simulation, 600);

  const finalDistance = distance(left, right);
  assert.ok(Number.isFinite(finalDistance));
  assert.ok(Math.abs(finalDistance - restLength) < Math.abs(initialDistance - restLength));
  assert.ok(finalDistance < 260);
});

test('nodes remain inside canvas boundaries under large velocities', () => {
  const simulation = createGraphSimulation([graphNode(1), graphNode(2, 4)], []);
  simulation.nodes[0].vx = -1000;
  simulation.nodes[0].vy = -1000;
  simulation.nodes[1].vx = 1000;
  simulation.nodes[1].vy = 1000;

  for (let index = 0; index < 80; index += 1) stepGraphSimulation(simulation, 3);

  for (const node of simulation.nodes) {
    const radius = Math.max(node.radius, MIN_COLLISION_RADIUS);
    assert.ok(node.x >= radius && node.x <= simulation.width - radius);
    assert.ok(node.y >= radius && node.y <= simulation.height - radius);
  }
});

test('pin stays exact, move clamps, and release allows motion', () => {
  const simulation = createGraphSimulation([graphNode(1), graphNode(2)], [graphEdge(1, 2)]);
  const pinned = pinGraphNode(simulation, 1, 120, 140);
  assert.ok(pinned);

  for (let index = 0; index < 40; index += 1) stepGraphSimulation(simulation);
  assert.equal(pinned.x, 120);
  assert.equal(pinned.y, 140);
  assert.equal(pinned.fixedX, 120);
  assert.equal(pinned.fixedY, 140);

  moveGraphNode(simulation, 1, -1000, 5000);
  const radius = Math.max(pinned.radius, MIN_COLLISION_RADIUS);
  assert.equal(pinned.x, radius);
  assert.equal(pinned.y, simulation.height - radius);

  const releasedX = pinned.x;
  const releasedY = pinned.y;
  releaseGraphNode(simulation, 1);
  assert.equal(pinned.fixedX, undefined);
  assert.equal(pinned.fixedY, undefined);
  for (let index = 0; index < 12; index += 1) stepGraphSimulation(simulation);
  assert.ok(distance(pinned, { x: releasedX, y: releasedY }) > 0.01);
});

test('velocity decays when force heat is disabled', () => {
  const simulation = createGraphSimulation([graphNode(1)], []);
  const node = simulation.nodes[0];
  node.x = simulation.width / 2;
  node.y = simulation.height / 2;
  node.vx = 10;
  node.vy = 0;
  simulation.alpha = 0;
  const speeds: number[] = [];

  for (let index = 0; index < 8; index += 1) {
    stepGraphSimulation(simulation);
    speeds.push(Math.hypot(node.vx, node.vy));
  }

  for (let index = 1; index < speeds.length; index += 1) assert.ok(speeds[index] < speeds[index - 1]);
  simulation.frame = simulation.maxFrames;
  assert.equal(shouldContinueGraphSimulation(simulation), false);
});

test('zoomAtPoint preserves the graph coordinate below its anchor', () => {
  const view = { zoom: 1.2, pan: { x: 35, y: -20 } };
  const anchor = { x: 620, y: 280 };
  const graphPoint = screenToGraph(anchor, view);
  const nextView = zoomAtPoint(view, anchor, { nextZoom: 1.8 });
  const nextScreenPoint = graphToScreen(graphPoint, nextView);

  assertClose(nextScreenPoint.x, anchor.x);
  assertClose(nextScreenPoint.y, anchor.y);
});

test('zoomAtPoint clamps zoom to supported limits', () => {
  const view = { zoom: 1, pan: { x: 0, y: 0 } };
  assert.equal(zoomAtPoint(view, { x: 500, y: 320 }, { nextZoom: 99 }).zoom, MAX_GRAPH_ZOOM);
  assert.equal(zoomAtPoint(view, { x: 500, y: 320 }, { nextZoom: 0.01 }).zoom, MIN_GRAPH_ZOOM);
  assert.equal(zoomAtPoint(view, { x: 500, y: 320 }, { wheelDelta: Number.POSITIVE_INFINITY }).zoom, 1);
});

test('wheel delta normalization handles pixel, line, and page modes', () => {
  assert.equal(normalizeWheelDelta(3, 0, 640), 3);
  assert.equal(normalizeWheelDelta(3, 1, 640), 48);
  assert.equal(normalizeWheelDelta(3, 2, 640), 1920);
});

test('graphs above the dynamic node limit use deterministic static degradation', () => {
  const nodes = Array.from({ length: MAX_DYNAMIC_GRAPH_NODES + 1 }, (_, index) => graphNode(index + 1));
  const simulation = createGraphSimulation(nodes, []);
  assert.equal(simulation.dynamicEnabled, false);
  assert.equal(simulation.dynamicDegraded, true);
  assert.equal(simulation.alpha, 0);
  assert.equal(stepGraphSimulation(simulation).shouldContinue, false);
  assert.equal(simulation.nodes.length, MAX_DYNAMIC_GRAPH_NODES + 1);
});

test('reflow restores the deterministic layout and clears pins', () => {
  const nodes = [graphNode(1, 3), graphNode(2, 2), graphNode(3, 1)];
  const simulation = createGraphSimulation(nodes, [graphEdge(1, 2)]);
  const expected = createGraphSimulation(nodes, [graphEdge(1, 2)]);
  pinGraphNode(simulation, 1, 120, 140);
  moveGraphNode(simulation, 2, 850, 500);
  settleGraphSimulation(simulation, 20);

  reflowGraphSimulation(simulation);

  for (const node of nodes) {
    const actualNode = byId(simulation, node.id);
    const expectedNode = byId(expected, node.id);
    assert.equal(actualNode.x, expectedNode.x);
    assert.equal(actualNode.y, expectedNode.y);
    assert.equal(actualNode.fixedX, undefined);
    assert.equal(actualNode.fixedY, undefined);
  }
  assert.equal(simulation.frame, 0);
  assert.equal(simulation.alpha, 1);
});

test('graph pan clamping keeps both axes within supported bounds', () => {
  assert.deepEqual(clampGraphPan({ x: 9999, y: -9999 }), { x: 360, y: -240 });
  assert.deepEqual(clampGraphPan({ x: -9999, y: 9999 }), { x: -360, y: 240 });
});
