import cytoscape from 'cytoscape';
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import {
  GRAPH_LAYOUT_PADDING,
  buildGraphModel,
  createFcoseLayout,
  lockGraphNode,
  reflowGraph,
  releaseGraphNode,
  resolveVisualOverlaps,
  visibleEdgeIndexes,
} from './graphCytoscape';
import type { GraphEdge, GraphNode } from './types';

const MIN_ZOOM = 0.45;
const MAX_ZOOM = 2.5;
const BUTTON_ZOOM_FACTOR = 1.15;
const KEYBOARD_PAN_STEP = 48;

export interface CytoscapeGraphCanvasHandle {
  zoomIn: () => void;
  zoomOut: () => void;
  panBy: (x: number, y: number) => void;
  reset: () => void;
  reflow: () => void;
  getZoom: () => number;
}

export interface CytoscapeGraphCanvasProps {
  nodes: GraphNode[];
  allEdges: GraphEdge[];
  visibleEdges: GraphEdge[];
  selectedNodeId: number | null;
  selectedMemoryId: number | null;
  highlightedNodeIds: ReadonlySet<number>;
  relationColors: ReadonlyMap<string, string>;
  onSelectNode: (nodeId: number) => void;
  onZoomChange?: (zoom: number) => void;
  ariaLabel: string;
  className?: string;
  nodeAriaLabel?: (node: GraphNode) => string;
}

interface GraphTheme {
  text: string;
  line: string;
  raised: string;
  accent: string;
  accentInk: string;
  focus: string;
}

function clamp(value: number, min: number, max: number) {
  return Math.max(min, Math.min(max, value));
}

function cssVariable(styles: CSSStyleDeclaration, name: string, fallback = '') {
  return styles.getPropertyValue(name).trim() || fallback;
}

function readGraphTheme(element: HTMLElement): GraphTheme {
  const styles = window.getComputedStyle(element);
  const text = cssVariable(styles, '--wf-text', styles.color);
  const line = cssVariable(styles, '--wf-line', text);
  const raised = cssVariable(styles, '--wf-raised-strong', cssVariable(styles, '--wf-raised', 'transparent'));
  const accent = cssVariable(styles, '--wf-accent', line);
  const accentInk = cssVariable(styles, '--wf-accent-ink', text);
  const focus = cssVariable(styles, '--wf-focus', accent);
  return { text, line, raised, accent, accentInk, focus };
}

function graphStyles(theme: GraphTheme, reducedMotion: boolean): cytoscape.StylesheetJson {
  const transitionDuration = reducedMotion ? 0 : 180;
  return [
    {
      selector: 'node',
      style: {
        width: 'data(visualWidth)',
        height: 'data(diameter)',
        label: 'data(label)',
        color: theme.text,
        'background-color': theme.raised,
        'border-color': theme.line,
        'border-width': 3,
        'font-size': 12,
        'font-weight': 700,
        'text-valign': 'center',
        'text-halign': 'center',
        'text-max-width': '112px',
        'text-wrap': 'ellipsis',
        'overlay-opacity': 0,
        'transition-property': 'opacity, border-width, border-color, background-color',
        'transition-duration': transitionDuration,
      },
    },
    {
      selector: 'edge',
      style: {
        width: 2.25,
        opacity: 0.32,
        'curve-style': 'bezier',
        'line-color': theme.line,
        'overlay-opacity': 0,
        'transition-property': 'opacity, width, line-color',
        'transition-duration': transitionDuration,
      },
    },
    { selector: 'edge.graph-edge--hidden', style: { display: 'none' } },
    { selector: 'edge.related', style: { opacity: 0.9, width: 3 } },
    { selector: 'edge.hovered', style: { opacity: 1, width: 3.5 } },
    { selector: 'edge.dimmed', style: { opacity: 0.1 } },
    { selector: 'node.dimmed', style: { opacity: 0.16 } },
    {
      selector: 'node.highlighted',
      style: {
        color: theme.accentInk,
        'background-color': theme.accent,
        'border-color': theme.accent,
        'border-width': 4,
      },
    },
    {
      selector: 'node.selected',
      style: {
        color: theme.accentInk,
        'background-color': theme.accent,
        'border-color': theme.focus,
        'border-width': 5,
      },
    },
    {
      selector: 'node.dragging, node.keyboard-focused',
      style: {
        color: theme.accentInk,
        'background-color': theme.accent,
        'border-color': theme.focus,
        'border-width': 6,
      },
    },
  ];
}

function graphNodeId(element: cytoscape.NodeSingular) {
  return Number(element.data('graphNodeId'));
}

function findGraphNode(cy: cytoscape.Core, nodeId: number): cytoscape.NodeSingular | null {
  const matches = cy.nodes().filter((node) => graphNodeId(node) === nodeId);
  return matches.length ? matches.first() as cytoscape.NodeSingular : null;
}

function activeNeighborhoodNodeId(hoveredNodeId: number | null, focusedNodeId: number | null) {
  return hoveredNodeId ?? focusedNodeId;
}

function applyNeighborhoodState(
  cy: cytoscape.Core,
  hoveredNodeId: number | null,
  focusedNodeId: number | null,
  selectedNodeId: number | null,
  highlightedNodeIds: ReadonlySet<number>,
) {
  cy.elements().removeClass('hovered dimmed');
  const activeNodeId = activeNeighborhoodNodeId(hoveredNodeId, focusedNodeId);
  if (activeNodeId === null) return;
  const activeNode = findGraphNode(cy, activeNodeId);
  if (!activeNode) return;

  const connectedEdges = activeNode.connectedEdges(':visible');
  const neighborNodes = connectedEdges.connectedNodes();
  connectedEdges.addClass('hovered');
  cy.edges(':visible').difference(connectedEdges).addClass('dimmed');
  cy.nodes().forEach((node) => {
    const nodeId = graphNodeId(node);
    const preserved = nodeId === activeNodeId
      || neighborNodes.contains(node)
      || nodeId === selectedNodeId
      || highlightedNodeIds.has(nodeId)
      || node.hasClass('selected')
      || node.hasClass('highlighted');
    node.toggleClass('dimmed', !preserved);
  });
}

function applySelectionState(
  cy: cytoscape.Core,
  selectedNodeId: number | null,
  selectedMemoryId: number | null,
  highlightedNodeIds: ReadonlySet<number>,
) {
  cy.elements().removeClass('selected highlighted related');
  cy.nodes().forEach((node) => {
    const nodeId = graphNodeId(node);
    node.toggleClass('selected', nodeId === selectedNodeId);
    node.toggleClass('highlighted', highlightedNodeIds.has(nodeId) || Boolean(node.data('highlighted')));
  });
  const noSelection = selectedNodeId === null && selectedMemoryId === null;
  cy.edges().forEach((edge) => {
    const related = noSelection
      || (selectedNodeId !== null && (graphNodeId(edge.source()) === selectedNodeId || graphNodeId(edge.target()) === selectedNodeId))
      || (selectedNodeId === null && selectedMemoryId !== null && Number(edge.data('memoryId')) === selectedMemoryId);
    edge.toggleClass('related', related);
  });
}

function applyEdgeVisibility(cy: cytoscape.Core, allEdges: GraphEdge[], visibleEdges: GraphEdge[]) {
  const visibleIndexes = visibleEdgeIndexes(allEdges, visibleEdges);
  cy.batch(() => {
    cy.edges().forEach((edge) => {
      const visible = visibleIndexes.has(Number(edge.data('edgeIndex')));
      edge.data('visible', visible ? 1 : 0);
      edge.toggleClass('graph-edge--hidden', !visible);
    });
  });
}

function applyRelationColors(cy: cytoscape.Core, relationColors: ReadonlyMap<string, string>) {
  cy.edges().forEach((edge) => {
    const color = relationColors.get(String(edge.data('relationType') || 'related'));
    if (color) edge.style('line-color', color);
    else edge.removeStyle('line-color');
  });
}

function usePrefersReducedMotion() {
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)');
    const handleChange = (event: MediaQueryListEvent) => setReducedMotion(event.matches);
    setReducedMotion(media.matches);
    media.addEventListener('change', handleChange);
    return () => media.removeEventListener('change', handleChange);
  }, []);

  return reducedMotion;
}

export const CytoscapeGraphCanvas = forwardRef<CytoscapeGraphCanvasHandle, CytoscapeGraphCanvasProps>(function CytoscapeGraphCanvas({
  nodes,
  allEdges,
  visibleEdges,
  selectedNodeId,
  selectedMemoryId,
  highlightedNodeIds,
  relationColors,
  onSelectNode,
  onZoomChange,
  ariaLabel,
  className,
  nodeAriaLabel,
}, forwardedRef) {
  const rootRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const elementsRef = useRef<cytoscape.ElementDefinition[]>([]);
  const dynamicEnabledRef = useRef(true);
  const layoutRef = useRef<cytoscape.Layouts | null>(null);
  const focusButtonsRef = useRef(new Map<number, HTMLButtonElement>());
  const focusSyncFrameRef = useRef<number | null>(null);
  const hoveredNodeIdRef = useRef<number | null>(null);
  const focusedNodeIdRef = useRef<number | null>(null);
  const selectedNodeIdRef = useRef(selectedNodeId);
  const highlightedNodeIdsRef = useRef(highlightedNodeIds);
  const allEdgesRef = useRef(allEdges);
  const visibleEdgesRef = useRef(visibleEdges);
  const relationColorsRef = useRef(relationColors);
  const onSelectNodeRef = useRef(onSelectNode);
  const onZoomChangeRef = useRef(onZoomChange);
  const reducedMotion = usePrefersReducedMotion();
  const reducedMotionRef = useRef(reducedMotion);
  const [focusedNodeId, setFocusedNodeId] = useState<number | null>(null);

  selectedNodeIdRef.current = selectedNodeId;
  highlightedNodeIdsRef.current = highlightedNodeIds;
  allEdgesRef.current = allEdges;
  visibleEdgesRef.current = visibleEdges;
  relationColorsRef.current = relationColors;
  onSelectNodeRef.current = onSelectNode;
  onZoomChangeRef.current = onZoomChange;
  reducedMotionRef.current = reducedMotion;

  const syncFocusButtons = useCallback((cy = cyRef.current) => {
    if (!cy || cy.destroyed()) return;
    cy.nodes().forEach((node) => {
      const button = focusButtonsRef.current.get(graphNodeId(node));
      if (!button) return;
      const position = node.renderedPosition();
      const size = Math.max(44, node.renderedOuterWidth() + 12, node.renderedOuterHeight() + 12);
      button.style.width = `${size}px`;
      button.style.height = `${size}px`;
      button.style.transform = `translate(${position.x - size / 2}px, ${position.y - size / 2}px)`;
      button.style.display = node.visible() ? 'block' : 'none';
    });
  }, []);

  const scheduleFocusSync = useCallback((cy = cyRef.current) => {
    if (!cy || cy.destroyed() || focusSyncFrameRef.current !== null) return;
    focusSyncFrameRef.current = window.requestAnimationFrame(() => {
      focusSyncFrameRef.current = null;
      syncFocusButtons(cy);
    });
  }, [syncFocusButtons]);

  const applyCurrentNeighborhood = useCallback((cy = cyRef.current) => {
    if (!cy || cy.destroyed()) return;
    applyNeighborhoodState(
      cy,
      hoveredNodeIdRef.current,
      focusedNodeIdRef.current,
      selectedNodeIdRef.current,
      highlightedNodeIdsRef.current,
    );
  }, []);

  const runLayout = useCallback(() => {
    const cy = cyRef.current;
    if (!cy || cy.destroyed()) return;

    const projectFitAndSync = () => {
      resolveVisualOverlaps(cy);
      cy.fit(cy.nodes(), GRAPH_LAYOUT_PADDING);
      onZoomChangeRef.current?.(cy.zoom());
      scheduleFocusSync(cy);
    };

    const fitPresetSeed = (restoreSeed: boolean) => {
      if (restoreSeed) reflowGraph(cy, elementsRef.current, { runLayout: false });
      cy.layout({ name: 'preset', fit: false }).run();
      projectFitAndSync();
    };

    try {
      layoutRef.current?.stop();
      layoutRef.current = null;
      reflowGraph(cy, elementsRef.current, { runLayout: false });
      if (!dynamicEnabledRef.current) {
        fitPresetSeed(false);
        return;
      }

      const layoutElements = cy.nodes().union(cy.edges('[visible = 1]'));
      const layout = createFcoseLayout(cy, elementsRef.current, { animate: false, fit: false }, layoutElements);
      layoutRef.current = layout;
      layout.one('layoutstop', () => {
        if (layoutRef.current === layout) layoutRef.current = null;
        projectFitAndSync();
      });
      layout.run();
    } catch {
      try {
        layoutRef.current?.stop();
      } catch {
        // The preset fallback below is independent from the failed layout instance.
      }
      layoutRef.current = null;
      try {
        fitPresetSeed(true);
      } catch {
        try {
          projectFitAndSync();
        } catch {
          // Keep the Cytoscape instance alive for effect cleanup even if fitting also fails.
        }
      }
    }
  }, [scheduleFocusSync]);

  const zoomAroundCenter = useCallback((factor: number) => {
    const cy = cyRef.current;
    if (!cy || cy.destroyed()) return;
    const nextZoom = clamp(cy.zoom() * factor, MIN_ZOOM, MAX_ZOOM);
    cy.zoom({ level: nextZoom, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  }, []);

  const resetCamera = useCallback(() => {
    const cy = cyRef.current;
    if (!cy || cy.destroyed()) return;
    cy.reset();
  }, []);

  useImperativeHandle(forwardedRef, () => ({
    zoomIn: () => zoomAroundCenter(BUTTON_ZOOM_FACTOR),
    zoomOut: () => zoomAroundCenter(1 / BUTTON_ZOOM_FACTOR),
    panBy: (x, y) => {
      const cy = cyRef.current;
      if (!cy || cy.destroyed()) return;
      cy.panBy({ x, y });
    },
    reset: resetCamera,
    reflow: runLayout,
    getZoom: () => cyRef.current?.zoom() ?? 1,
  }), [resetCamera, runLayout, zoomAroundCenter]);

  useEffect(() => {
    const root = rootRef.current;
    const viewport = viewportRef.current;
    if (!root || !viewport) return undefined;

    const model = buildGraphModel(nodes, allEdges, visibleEdgesRef.current, relationColorsRef.current);
    const elements = model.elements;
    elementsRef.current = elements;
    dynamicEnabledRef.current = model.dynamicEnabled;
    const cy = cytoscape({
      container: viewport,
      elements,
      style: graphStyles(readGraphTheme(root), reducedMotionRef.current),
      minZoom: MIN_ZOOM,
      maxZoom: MAX_ZOOM,
      boxSelectionEnabled: false,
      autolock: false,
      autoungrabify: false,
      selectionType: 'single',
      userPanningEnabled: true,
      userZoomingEnabled: true,
    });
    cyRef.current = cy;
    applyEdgeVisibility(cy, allEdgesRef.current, visibleEdgesRef.current);
    applySelectionState(cy, selectedNodeIdRef.current, selectedMemoryId, highlightedNodeIdsRef.current);
    applyRelationColors(cy, relationColorsRef.current);
    applyCurrentNeighborhood(cy);

    const handleTapNode: cytoscape.EventHandler = (event) => {
      const node = event.target as cytoscape.NodeSingular;
      onSelectNodeRef.current(graphNodeId(node));
    };
    const handleMouseOverNode: cytoscape.EventHandler = (event) => {
      hoveredNodeIdRef.current = graphNodeId(event.target as cytoscape.NodeSingular);
      applyCurrentNeighborhood(cy);
    };
    const handleMouseOutNode: cytoscape.EventHandler = () => {
      hoveredNodeIdRef.current = null;
      applyCurrentNeighborhood(cy);
    };
    const handleTapStartNode: cytoscape.EventHandler = (event) => {
      releaseGraphNode(cy, graphNodeId(event.target as cytoscape.NodeSingular));
    };
    const handleGrabNode: cytoscape.EventHandler = (event) => {
      (event.target as cytoscape.NodeSingular).addClass('dragging');
    };
    const handleFreeNode: cytoscape.EventHandler = (event) => {
      const node = event.target as cytoscape.NodeSingular;
      node.removeClass('dragging');
      lockGraphNode(cy, graphNodeId(node), node.position());
      scheduleFocusSync(cy);
    };
    const handleViewportChange = () => scheduleFocusSync(cy);
    const handleZoom = () => {
      onZoomChangeRef.current?.(cy.zoom());
      scheduleFocusSync(cy);
    };

    cy.on('tap', 'node', handleTapNode);
    cy.on('mouseover', 'node', handleMouseOverNode);
    cy.on('mouseout', 'node', handleMouseOutNode);
    cy.on('tapstart', 'node', handleTapStartNode);
    cy.on('grab', 'node', handleGrabNode);
    cy.on('free', 'node', handleFreeNode);
    cy.on('render position pan resize', handleViewportChange);
    cy.on('zoom', handleZoom);

    const resizeObserver = new ResizeObserver(() => {
      if (cy.destroyed()) return;
      cy.resize();
      scheduleFocusSync(cy);
    });
    resizeObserver.observe(root);

    const applyTheme = () => {
      if (cy.destroyed()) return;
      cy.style(graphStyles(readGraphTheme(root), reducedMotionRef.current)).update();
      applyRelationColors(cy, relationColorsRef.current);
    };
    const themeObserver = new MutationObserver(applyTheme);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style', 'data-theme'] });

    scheduleFocusSync(cy);
    runLayout();

    return () => {
      resizeObserver.disconnect();
      themeObserver.disconnect();
      try {
        layoutRef.current?.stop();
      } catch {
        // Cleanup must still destroy the Cytoscape instance after a layout failure.
      }
      layoutRef.current = null;
      elementsRef.current = [];
      dynamicEnabledRef.current = true;
      if (focusSyncFrameRef.current !== null) {
        window.cancelAnimationFrame(focusSyncFrameRef.current);
        focusSyncFrameRef.current = null;
      }
      cy.off('tap', 'node', handleTapNode);
      cy.off('mouseover', 'node', handleMouseOverNode);
      cy.off('mouseout', 'node', handleMouseOutNode);
      cy.off('tapstart', 'node', handleTapStartNode);
      cy.off('grab', 'node', handleGrabNode);
      cy.off('free', 'node', handleFreeNode);
      cy.off('render position pan resize', handleViewportChange);
      cy.off('zoom', handleZoom);
      cy.destroy();
      if (cyRef.current === cy) cyRef.current = null;
    };
  }, [allEdges, applyCurrentNeighborhood, nodes, runLayout, scheduleFocusSync]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || cy.destroyed()) return;
    applyEdgeVisibility(cy, allEdges, visibleEdges);
    applyCurrentNeighborhood(cy);
    scheduleFocusSync(cy);
  }, [allEdges, applyCurrentNeighborhood, scheduleFocusSync, visibleEdges]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || cy.destroyed()) return;
    applySelectionState(cy, selectedNodeId, selectedMemoryId, highlightedNodeIds);
    applyCurrentNeighborhood(cy);
  }, [applyCurrentNeighborhood, highlightedNodeIds, selectedMemoryId, selectedNodeId]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || cy.destroyed()) return;
    applyRelationColors(cy, relationColors);
  }, [relationColors]);

  useEffect(() => {
    const cy = cyRef.current;
    const root = rootRef.current;
    if (!cy || cy.destroyed() || !root) return;
    cy.style(graphStyles(readGraphTheme(root), reducedMotion)).update();
    applyRelationColors(cy, relationColorsRef.current);
  }, [reducedMotion]);

  const handleCanvasKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const cy = cyRef.current;
    if (!cy || cy.destroyed()) return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); cy.panBy({ x: -KEYBOARD_PAN_STEP, y: 0 }); }
    if (event.key === 'ArrowRight') { event.preventDefault(); cy.panBy({ x: KEYBOARD_PAN_STEP, y: 0 }); }
    if (event.key === 'ArrowUp') { event.preventDefault(); cy.panBy({ x: 0, y: -KEYBOARD_PAN_STEP }); }
    if (event.key === 'ArrowDown') { event.preventDefault(); cy.panBy({ x: 0, y: KEYBOARD_PAN_STEP }); }
    if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomAroundCenter(BUTTON_ZOOM_FACTOR); }
    if (event.key === '-') { event.preventDefault(); zoomAroundCenter(1 / BUTTON_ZOOM_FACTOR); }
    if (event.key === '0') { event.preventDefault(); resetCamera(); }
  };

  const focusNodes = [...nodes].sort((left, right) => left.id - right.id);

  return (
    <div
      ref={rootRef}
      className={`graph-canvas cytoscape-graph-canvas${className ? ` ${className}` : ''}`}
      role="application"
      tabIndex={0}
      aria-label={ariaLabel}
      onKeyDown={handleCanvasKeyDown}
    >
      <div ref={viewportRef} className="graph-cytoscape-viewport" aria-hidden="true" />
      <div className="graph-focus-layer">
        {focusNodes.map((node) => (
          <button
            key={node.id}
            ref={(element) => {
              if (element) focusButtonsRef.current.set(node.id, element);
              else focusButtonsRef.current.delete(node.id);
            }}
            type="button"
            className="graph-focus-node"
            aria-label={nodeAriaLabel?.(node) ?? `${node.label || node.id}, ${node.type || 'unknown'}`}
            onFocus={() => {
              focusedNodeIdRef.current = node.id;
              setFocusedNodeId(node.id);
              const cy = cyRef.current;
              if (cy && !cy.destroyed()) findGraphNode(cy, node.id)?.addClass('keyboard-focused');
              applyCurrentNeighborhood();
            }}
            onBlur={() => {
              focusedNodeIdRef.current = null;
              setFocusedNodeId((current) => current === node.id ? null : current);
              const cy = cyRef.current;
              if (cy && !cy.destroyed()) findGraphNode(cy, node.id)?.removeClass('keyboard-focused');
              applyCurrentNeighborhood();
            }}
            onClick={() => onSelectNodeRef.current(node.id)}
            style={{ opacity: focusedNodeId === node.id ? 1 : 0 }}
          />
        ))}
      </div>
    </div>
  );
});
