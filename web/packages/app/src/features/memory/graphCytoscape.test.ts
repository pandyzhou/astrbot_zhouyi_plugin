import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';
import { test } from 'node:test';
import type cytoscape from 'cytoscape';
import type { GraphEdge, GraphNode } from './types';
import {
  GRAPH_HEIGHT,
  GRAPH_RELATION_COLORS,
  GRAPH_WIDTH,
  MAX_DYNAMIC_GRAPH_NODES,
  MIN_NODE_RADIUS,
  buildGraphElements,
  buildGraphModel,
  buildVisibleTopology,
  createFcoseLayout,
  createFcoseLayoutOptions,
  createHeadlessGraph,
  lockGraphNode,
  moveLockedGraphNode,
  reflowGraph,
  releaseGraphNode,
  resolveVisualOverlaps,
  setVisibleEdges,
  updateManualLocks,
  visibleEdgeIndexes,
  type GraphElementEdgeData,
  type GraphElementNodeData,
} from './graphCytoscape';

function graphNode(id: number, weight = 1, degree = 0, label = `node-${id}`): GraphNode {
  return { id, label, weight, degree };
}

function graphEdge(source: number, target: number, id?: number, relationType = 'related', memoryId?: number): GraphEdge {
  return { id, source, target, relation_type: relationType, memory_id: memoryId, weight: 1 };
}

function nodeDefinitions(elements: readonly cytoscape.ElementDefinition[]): cytoscape.NodeDefinition[] {
  return elements.filter((element): element is cytoscape.NodeDefinition => element.group === 'nodes');
}

function edgeDefinitions(elements: readonly cytoscape.ElementDefinition[]): cytoscape.EdgeDefinition[] {
  return elements.filter((element): element is cytoscape.EdgeDefinition => element.group === 'edges');
}

function nodeData(element: cytoscape.NodeDefinition): GraphElementNodeData {
  return element.data as GraphElementNodeData;
}

function edgeData(element: cytoscape.EdgeDefinition): GraphElementEdgeData {
  return element.data as GraphElementEdgeData;
}

function semanticSnapshot(elements: readonly cytoscape.ElementDefinition[]) {
  return elements.map((element) => {
    if (element.group === 'nodes') {
      const data = nodeData(element as cytoscape.NodeDefinition);
      return {
        group: element.group,
        id: data.id,
        radius: data.radius,
        label: data.label,
        position: element.position,
      };
    }
    const data = edgeData(element as cytoscape.EdgeDefinition);
    return {
      group: element.group,
      id: data.id,
      source: data.source,
      target: data.target,
      relationType: data.relationType,
      relationClass: data.relationClass,
      relationColor: data.relationColor,
    };
  });
}

function assertFinitePositions(core: cytoscape.Core): void {
  core.nodes().forEach((node) => {
    const position = node.position();
    assert.ok(Number.isFinite(position.x), `node ${node.id()} x is not finite`);
    assert.ok(Number.isFinite(position.y), `node ${node.id()} y is not finite`);
  });
}

function visualOverlapPairs(core: cytoscape.Core, gap = 14, tolerance = 0.01): string[] {
  const nodes = core.nodes().sort((left, right) => Number(left.id()) - Number(right.id()));
  const overlaps: string[] = [];
  for (let leftIndex = 0; leftIndex < nodes.length; leftIndex += 1) {
    const left = nodes[leftIndex];
    const leftPosition = left.position();
    const leftHalfWidth = Number(left.data('visualWidth')) / 2;
    const leftHalfHeight = Number(left.data('diameter')) / 2;
    for (let rightIndex = leftIndex + 1; rightIndex < nodes.length; rightIndex += 1) {
      const right = nodes[rightIndex];
      const rightPosition = right.position();
      const rightHalfWidth = Number(right.data('visualWidth')) / 2;
      const rightHalfHeight = Number(right.data('diameter')) / 2;
      const overlapX = leftHalfWidth + rightHalfWidth + gap - Math.abs(rightPosition.x - leftPosition.x);
      const overlapY = leftHalfHeight + rightHalfHeight + gap - Math.abs(rightPosition.y - leftPosition.y);
      if (overlapX > tolerance && overlapY > tolerance) overlaps.push(`${left.id()}-${right.id()}`);
    }
  }
  return overlaps;
}

function nodeDistance(core: cytoscape.Core, leftId: number, rightId: number): number {
  const left = core.getElementById(String(leftId)).position();
  const right = core.getElementById(String(rightId)).position();
  return Math.hypot(right.x - left.x, right.y - left.y);
}

function sortedNodePositions(core: cytoscape.Core): cytoscape.Position[] {
  const nodes: cytoscape.NodeSingular[] = [];
  core.nodes().forEach((node) => {
    nodes.push(node);
  });
  nodes.sort((left, right) => Number(left.id()) - Number(right.id()));
  return nodes.map((node) => node.position());
}

test('stable ranking and golden-angle seeds do not depend on input order', () => {
  const nodes = [graphNode(9, 2, 1), graphNode(2, 5, 3), graphNode(6, 5, 1), graphNode(4, 0, 8)];
  const edges = [graphEdge(9, 2, 7, 'supports'), graphEdge(2, 6, 3, 'conflicts')];
  const forward = buildGraphElements(nodes, edges);
  const reversed = buildGraphElements([...nodes].reverse(), [...edges].reverse());

  assert.deepEqual(semanticSnapshot(forward), semanticSnapshot(reversed));
  assert.deepEqual(nodeDefinitions(forward).map((element) => nodeData(element).graphNodeId), [2, 6, 9, 4]);

  const first = nodeDefinitions(forward)[0];
  const expectedProgress = Math.sqrt(0.65 / nodes.length);
  assert.equal(first.position?.x, GRAPH_WIDTH / 2 + GRAPH_WIDTH * 0.405 * expectedProgress);
  assert.equal(first.position?.y, GRAPH_HEIGHT / 2);
});

test('equal weight and degree use numeric id as stable tie-breaker', () => {
  const elements = buildGraphElements([graphNode(8, 2, 3), graphNode(3, 2, 3), graphNode(5, 2, 3)], []);
  assert.deepEqual(nodeDefinitions(elements).map((element) => nodeData(element).graphNodeId), [3, 5, 8]);
});

test('missing endpoints are filtered while valid edges remain sorted', () => {
  const elements = buildGraphElements(
    [graphNode(1), graphNode(2)],
    [graphEdge(1, 99, 4), graphEdge(2, 1, 3), graphEdge(1, 2, 2), graphEdge(98, 2, 1)],
  );
  const edges = edgeDefinitions(elements).map(edgeData);
  assert.equal(edges.length, 2);
  assert.deepEqual(edges.map((edge) => [edge.source, edge.target]), [['1', '2'], ['2', '1']]);
});

test('node data reserves visual space for minimum size and labels', () => {
  const longLabel = '这是一个非常长的知识图谱节点标签用于占位';
  const elements = buildGraphElements([graphNode(1, 0, 0, longLabel), graphNode(2, 9, 12, 'short')], []);
  const [large, small] = nodeDefinitions(elements).map(nodeData);
  const longLabelNode = large.graphNodeId === 1 ? large : small;
  const weightedNode = large.graphNodeId === 2 ? large : small;

  assert.equal(longLabelNode.label, `${longLabel.slice(0, 15)}…`);
  assert.equal(longLabelNode.radius, MIN_NODE_RADIUS);
  assert.ok(longLabelNode.labelWidth > longLabelNode.diameter);
  assert.equal(longLabelNode.visualWidth, longLabelNode.labelWidth);
  assert.ok(weightedNode.radius >= MIN_NODE_RADIUS && weightedNode.radius <= 24);
  assert.ok(weightedNode.repulsion > 0 && Number.isFinite(weightedNode.repulsion));
});

test('relation classes and colors are deterministic and carried by edge data', () => {
  const nodes = [graphNode(1), graphNode(2), graphNode(3)];
  const edges = [graphEdge(1, 2, 1, 'supports'), graphEdge(2, 3, 2, 'conflicts'), graphEdge(3, 1, 3)];
  const model = buildGraphModel(nodes, edges);
  assert.deepEqual([...model.relationColors.keys()], ['conflicts', 'related', 'supports']);
  assert.deepEqual([...model.relationColors.values()], GRAPH_RELATION_COLORS.slice(0, 3));

  for (const element of model.edgeElements) {
    const data = edgeData(element);
    assert.match(data.relationClass, /^graph-relation-\d+$/);
    assert.equal(data.relationColor, model.relationColors.get(data.relationType));
    assert.ok(String(element.classes).includes(data.relationClass));
  }
});

test('visible topology keeps only visible valid edge indexes', () => {
  const nodes = [graphNode(3), graphNode(1), graphNode(2), graphNode(2)];
  const duplicate = graphEdge(1, 2, 7, 'supports');
  const allEdges = [
    duplicate,
    { ...duplicate },
    graphEdge(2, 3, 8, 'conflicts'),
    graphEdge(3, 99, 9, 'supports'),
  ];
  const topology = buildVisibleTopology(nodes, allEdges, [{ ...duplicate }, allEdges[2], allEdges[3]]);

  assert.deepEqual(topology.nodeIds, [1, 2, 3]);
  assert.deepEqual([...topology.edgeIndexes], [0, 2]);
});

test('manual lock bookkeeping is immutable and reflow clears it', () => {
  const initial = new Set([1]);
  const locked = updateManualLocks(initial, { type: 'lock', nodeId: 2 });
  const cleared = updateManualLocks(locked, { type: 'reflow' });

  assert.deepEqual([...initial], [1]);
  assert.deepEqual([...locked], [1, 2]);
  assert.deepEqual([...cleared], []);
});

test('fCoSE options use deterministic default mode, honor animate, and clip forces', (t) => {
  const model = buildGraphModel([graphNode(1, 1, 400), graphNode(2, 4, 2)], [graphEdge(1, 2, 1)]);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());
  const options = createFcoseLayoutOptions(model.elements);
  const node = core.getElementById('1');
  const edge = core.edges()[0];

  assert.equal(options.name, 'fcose');
  assert.equal(options.quality, 'default');
  assert.equal(options.randomize, false);
  assert.equal(options.animate, false);
  assert.equal(createFcoseLayoutOptions(model.elements, { animate: true }).animate, true);
  assert.equal(options.fit, true);
  assert.equal(options.packComponents, true);
  assert.equal(options.nodeDimensionsIncludeLabels, true);
  assert.equal(options.uniformNodeDimensions, false);
  assert.ok(Number.isFinite(options.nodeRepulsion(node)));
  assert.ok(Number.isFinite(options.idealEdgeLength(edge)));
  assert.ok(Number.isFinite(options.edgeElasticity(edge)));

  node.data('repulsion', Number.POSITIVE_INFINITY);
  edge.data('idealLength', Number.NaN);
  edge.data('elasticity', Number.NEGATIVE_INFINITY);
  assert.equal(options.nodeRepulsion(node), 5_000);
  assert.equal(options.idealEdgeLength(edge), 118);
  assert.ok(options.edgeElasticity(edge) >= 0.32 && options.edgeElasticity(edge) <= 0.62);
});

test('visible edge filtering changes state without removing layout edges', (t) => {
  const nodes = [graphNode(1), graphNode(2), graphNode(3)];
  const allEdges = [
    graphEdge(1, 2, 1, 'supports'),
    graphEdge(2, 3, 2, 'conflicts'),
    graphEdge(3, 1, 3, 'supports'),
  ];
  const visibleEdges = allEdges.filter((edge) => edge.relation_type === 'supports');
  const model = buildGraphModel(nodes, allEdges, visibleEdges);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());

  assert.equal(model.edgeElements.length, allEdges.length);
  assert.equal(model.edgeElements.filter((element) => edgeData(element).visible === 1).length, 2);
  assert.deepEqual([...visibleEdgeIndexes(allEdges, visibleEdges)], [0, 2]);

  setVisibleEdges(core, allEdges, [allEdges[1]]);
  assert.equal(core.edges().length, allEdges.length);
  assert.equal(core.edges('[visible = 1]').length, 1);
  assert.equal(core.edges('[visible = 1]')[0].data('edgeIndex'), 1);
  assert.equal(core.edges('.graph-edge--hidden').length, 2);
});

test('lock, unconstrained move, finite fallback, and release are headless-testable', (t) => {
  const model = buildGraphModel([graphNode(1, 1, 0, 'wide-wide-wide-wide-label'), graphNode(2)], [graphEdge(1, 2)]);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());
  const node = lockGraphNode(core, 1, { x: 1375, y: -240 });
  assert.ok(node);
  assert.equal(node.locked(), true);
  assert.deepEqual(node.position(), { x: 1375, y: -240 });

  moveLockedGraphNode(core, 1, { x: -325, y: 910 });
  assert.deepEqual(node.position(), { x: -325, y: 910 });

  moveLockedGraphNode(core, 1, { x: Number.NaN, y: Number.POSITIVE_INFINITY });
  assert.deepEqual(node.position(), { x: -325, y: 910 });

  releaseGraphNode(core, 1);
  assert.equal(node.locked(), false);
});

test('reflow restores golden seeds and clears locks without changing elements', (t) => {
  const model = buildGraphModel([graphNode(1, 3), graphNode(2, 2), graphNode(3, 1)], [graphEdge(1, 2)]);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());
  const expected = new Map(model.nodeElements.map((element) => [String(element.data.id), element.position]));
  lockGraphNode(core, 1, { x: 120, y: 140 });
  moveLockedGraphNode(core, 2, { x: 850, y: 500 });
  const elementCount = core.elements().length;

  const layout = reflowGraph(core, model.elements, { runLayout: false });

  assert.equal(layout, undefined);
  assert.equal(core.elements().length, elementCount);
  core.nodes().forEach((node) => {
    assert.equal(node.locked(), false);
    assert.deepEqual(node.position(), expected.get(node.id()));
  });
});

test('fCoSE runs headlessly from golden seeds with finite positions', (t) => {
  const nodes = [graphNode(1, 5, 2), graphNode(2, 4, 2), graphNode(3, 1, 2), graphNode(4, 1, 2)];
  const edges = [graphEdge(1, 2, 1), graphEdge(1, 3, 2), graphEdge(2, 4, 3), graphEdge(3, 4, 4)];
  const model = buildGraphModel(nodes, edges);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());

  createFcoseLayout(core, model.elements, { animate: false, fit: false }).run();

  assertFinitePositions(core);
  assert.equal(core.nodes().length, nodes.length);
  assert.equal(core.edges().length, edges.length);
});

function createSharedNeighborFixture(): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes = [
    graphNode(1, 24, 47),
    graphNode(2, 22, 47),
    ...Array.from({ length: 46 }, (_, index) => graphNode(index + 3, 1, 2)),
  ];
  const edges = [
    graphEdge(1, 2, 1),
    ...Array.from({ length: 46 }, (_, index) => {
      const neighborId = index + 3;
      return [graphEdge(1, neighborId, index * 2 + 2), graphEdge(2, neighborId, index * 2 + 3)];
    }).flat(),
  ];
  return { nodes, edges };
}

test('shared-neighbor fCoSE projection separates all visual boxes', { timeout: 30_000 }, (t) => {
  const fixture = createSharedNeighborFixture();
  const model = buildGraphModel(fixture.nodes, fixture.edges);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());

  createFcoseLayout(core, model.elements, { animate: false, fit: false }).run();
  const result = resolveVisualOverlaps(core);

  const overlaps = visualOverlapPairs(core);
  const centerDistance = nodeDistance(core, 1, 2);
  assert.ok(result.initialOverlaps > 0);
  assert.equal(result.remainingOverlaps, 0, `remaining overlaps=${result.remainingOverlaps}`);
  assert.equal(overlaps.length, 0, `visual overlaps=${overlaps.length}, center distance=${centerDistance.toFixed(1)}`);
  assert.ok(centerDistance > 50, `center distance=${centerDistance.toFixed(1)}`);
});

test('visual overlap projection preserves locks and assigns full displacement to the unlocked node', (t) => {
  const model = buildGraphModel([graphNode(1), graphNode(2)], []);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());
  core.nodes().positions(() => ({ x: 300, y: 200 }));
  const locked = core.getElementById('1');
  const unlocked = core.getElementById('2');
  locked.lock();
  const lockedPosition = locked.position();

  const result = resolveVisualOverlaps(core);

  assert.deepEqual(locked.position(), lockedPosition);
  assert.notDeepEqual(unlocked.position(), lockedPosition);
  assert.equal(result.remainingOverlaps, 0);
  assert.equal(visualOverlapPairs(core).length, 0);
});

test('visual overlap projection leaves two locked nodes unchanged', (t) => {
  const model = buildGraphModel([graphNode(1), graphNode(2)], []);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());
  core.nodes().positions(() => ({ x: 300, y: 200 }));
  core.nodes().lock();
  const before = core.nodes().map((node) => node.position());

  const result = resolveVisualOverlaps(core);

  assert.deepEqual(core.nodes().map((node) => node.position()), before);
  assert.equal(result.iterations, 1);
  assert.equal(result.remainingOverlaps, 1);
  assert.equal(result.movedNodeCount, 0);
});

test('fully coincident nodes resolve finitely and deterministically by numeric id', (t) => {
  const firstModel = buildGraphModel([graphNode(10), graphNode(2)], []);
  const secondModel = buildGraphModel([graphNode(10), graphNode(2)], []);
  const first = createHeadlessGraph(firstModel.elements);
  const second = createHeadlessGraph(secondModel.elements);
  t.after(() => first.destroy());
  t.after(() => second.destroy());
  first.nodes().positions(() => ({ x: 400, y: 300 }));
  second.nodes().positions(() => ({ x: 400, y: 300 }));

  const firstResult = resolveVisualOverlaps(first);
  const secondResult = resolveVisualOverlaps(second);
  const firstPositions = sortedNodePositions(first);
  const secondPositions = sortedNodePositions(second);

  assert.deepEqual(firstPositions, secondPositions);
  assert.ok(firstResult.iterations <= 12);
  assert.deepEqual(firstResult, secondResult);
  assert.equal(firstResult.remainingOverlaps, 0);
  assertFinitePositions(first);
  assert.ok(first.getElementById('2').position().y < first.getElementById('10').position().y);
});

function createBoundedFixture(nodeCount: number, edgeCount: number): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes = Array.from({ length: nodeCount }, (_, index) => graphNode(index + 1, (index % 9) + 1, 3));
  const edges: GraphEdge[] = [];
  for (let offset = 1; edges.length < edgeCount; offset += 1) {
    for (let index = 0; index < nodeCount && edges.length < edgeCount; index += 1) {
      const source = index + 1;
      const target = ((index + offset) % nodeCount) + 1;
      if (source !== target) edges.push(graphEdge(source, target, edges.length + 1, offset % 2 ? 'related' : 'supports'));
    }
  }
  return { nodes, edges };
}

function runHeadlessPerformanceCase(nodes: GraphNode[], edges: GraphEdge[]): number {
  const model = buildGraphModel(nodes, edges);
  const core = createHeadlessGraph(model.elements);
  try {
    const startedAt = performance.now();
    createFcoseLayout(core, model.elements, { animate: false, fit: false }).run();
    const duration = performance.now() - startedAt;
    assertFinitePositions(core);
    assert.equal(core.nodes().length, nodes.length);
    assert.equal(core.edges().length, edges.length);
    return duration;
  } finally {
    core.destroy();
  }
}

test('default fCoSE keeps 48-node and 80-node fixtures headless-safe', { timeout: 30_000 }, () => {
  const shared = createSharedNeighborFixture();
  const bounded = createBoundedFixture(80, 120);
  const sharedDuration = runHeadlessPerformanceCase(shared.nodes, shared.edges);
  const boundedDuration = runHeadlessPerformanceCase(bounded.nodes, bounded.edges);

  assert.ok(sharedDuration < 15_000, `48-node layout took ${sharedDuration.toFixed(1)}ms`);
  assert.ok(boundedDuration < 15_000, `80-node layout took ${boundedDuration.toFixed(1)}ms`);
});

test('more than 80 nodes restore preset seeds and never run dynamic fCoSE', (t) => {
  const fixture = createBoundedFixture(MAX_DYNAMIC_GRAPH_NODES + 1, 120);
  const model = buildGraphModel(fixture.nodes, fixture.edges);
  const core = createHeadlessGraph(model.elements);
  t.after(() => core.destroy());
  const expected = new Map(model.nodeElements.map((element) => [String(element.data.id), element.position]));
  lockGraphNode(core, 1, { x: 120, y: 140 });
  moveLockedGraphNode(core, 2, { x: 850, y: 500 });

  const layout = reflowGraph(core, model.elements);

  assert.equal(model.dynamicEnabled, false);
  assert.equal(model.dynamicDegraded, true);
  assert.equal(layout, undefined);
  core.nodes().forEach((node) => {
    assert.equal(node.locked(), false);
    assert.deepEqual(node.position(), expected.get(node.id()));
  });
});
