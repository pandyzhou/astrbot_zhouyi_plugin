import type {
  MemoryConfigObject,
  MemoryConfigSchemaNode,
  MemoryConfigValue,
  MemoryFieldConstraint,
  MemoryProviderOptions,
} from '../../api/types';

export type MemoryFieldKind = 'boolean' | 'string' | 'integer' | 'number';

export interface MemoryConfigField {
  key: string;
  path: string[];
  kind: MemoryFieldKind;
  label: string;
  description: string;
  hint: string;
  defaultValue: MemoryConfigValue | undefined;
  options: Array<{ value: string; label: string; unavailable?: boolean }>;
  constraint: MemoryFieldConstraint;
  providerType?: 'llm' | 'embedding';
}

export interface MemoryConfigCategory {
  key: string;
  title: string;
  fields: MemoryConfigField[];
}

export interface ParsedMemoryConfigSchema {
  categories: MemoryConfigCategory[];
  fields: MemoryConfigField[];
}

export type MemoryConfigDraft = MemoryConfigObject;
export type MemoryValidationErrors = Record<string, string>;

interface MemoryConfigCategoryGroup {
  key: string;
  title: string;
  sources: readonly string[];
}

const CATEGORY_GROUPS: readonly MemoryConfigCategoryGroup[] = [
  { key: 'basics', title: '基础与模型', sources: ['basic', 'provider_settings'] },
  { key: 'conversation-recall', title: '会话与召回', sources: ['session_manager', 'recall_engine', 'fusion_strategy', 'retrieval'] },
  { key: 'memory-processing', title: '记忆处理', sources: ['reflection_engine', 'agent_tools', 'filtering_settings'] },
  { key: 'graph-weighting', title: '图记忆与权重', sources: ['graph_memory', 'importance_decay'] },
  { key: 'maintenance', title: '数据维护', sources: ['forgetting_agent', 'migration_settings', 'index_rebuild_settings', 'backup_settings'] },
];
const OTHER_CATEGORY_GROUP: MemoryConfigCategoryGroup = { key: 'other', title: '其他设置', sources: [] };
const CATEGORY_GROUP_BY_SOURCE = new Map(
  CATEGORY_GROUPS.flatMap((group) => group.sources.map((source) => [source, group] as const)),
);
const scalarTypes = new Set(['boolean', 'bool', 'string', 'integer', 'number', 'int', 'float']);

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function deepClone<T>(value: T): T {
  if (typeof structuredClone === 'function') return structuredClone(value);
  return JSON.parse(JSON.stringify(value)) as T;
}

export function pathKey(path: readonly string[]): string {
  return path.join('.');
}

export function getAtPath(value: unknown, path: readonly string[]): unknown {
  let current = value;
  for (const segment of path) {
    if (!isObject(current)) return undefined;
    current = current[segment];
  }
  return current;
}

export function setAtPath<T extends MemoryConfigObject>(value: T, path: readonly string[], nextValue: MemoryConfigValue): T {
  const next = deepClone(value);
  let current: MemoryConfigObject = next;
  path.forEach((segment, index) => {
    if (index === path.length - 1) {
      current[segment] = nextValue;
      return;
    }
    const child = current[segment];
    if (!isObject(child)) current[segment] = {};
    current = current[segment] as MemoryConfigObject;
  });
  return next;
}

function childProperties(node: MemoryConfigSchemaNode): Record<string, MemoryConfigSchemaNode> | null {
  if (isObject(node.properties)) return node.properties as Record<string, MemoryConfigSchemaNode>;
  if (isObject(node.items)) return node.items as Record<string, MemoryConfigSchemaNode>;
  return null;
}

function schemaProperties(
  schema: MemoryConfigSchemaNode | Record<string, MemoryConfigSchemaNode>,
  config: MemoryConfigObject,
) {
  const direct = childProperties(schema as MemoryConfigSchemaNode);
  if (direct) return direct;
  const entries = Object.entries(schema).filter(([, value]) => isObject(value)) as Array<[string, MemoryConfigSchemaNode]>;
  if (entries.length === 1) {
    const [wrapperKey, wrapperNode] = entries[0];
    const wrapped = childProperties(wrapperNode);
    if (wrapped && !(wrapperKey in config) && Object.keys(wrapped).some((key) => key in config)) return wrapped;
  }
  return Object.fromEntries(entries) as Record<string, MemoryConfigSchemaNode>;
}

function nodeKind(node: MemoryConfigSchemaNode, currentValue: unknown): MemoryFieldKind | null {
  const declared = typeof node.type === 'string' ? node.type.toLowerCase() : '';
  if (declared === 'int') return 'integer';
  if (declared === 'float') return 'number';
  if (declared === 'bool') return 'boolean';
  if (scalarTypes.has(declared)) return declared as MemoryFieldKind;
  if (typeof currentValue === 'boolean' || typeof node.default === 'boolean') return 'boolean';
  if (typeof currentValue === 'number' || typeof node.default === 'number') {
    return Number.isInteger(currentValue ?? node.default) ? 'integer' : 'number';
  }
  if (typeof currentValue === 'string' || typeof node.default === 'string' || node.options || node.enum) return 'string';
  return null;
}

function displayLabel(key: string, node: MemoryConfigSchemaNode): string {
  return String(node.label ?? node.title ?? node.description ?? key);
}

function normalizeConstraint(
  node: MemoryConfigSchemaNode,
  constraints: Record<string, MemoryFieldConstraint | Record<string, unknown>>,
  path: readonly string[],
): MemoryFieldConstraint {
  const nested = getAtPath(constraints, path);
  const direct = constraints[pathKey(path)];
  const external = isObject(direct) ? direct : isObject(nested) ? nested : {};
  const numberValue = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
  return {
    min: numberValue(external.min) ?? numberValue(external.minimum) ?? numberValue(node.min) ?? numberValue(node.minimum),
    max: numberValue(external.max) ?? numberValue(external.maximum) ?? numberValue(node.max) ?? numberValue(node.maximum),
    exclusive_min: numberValue(external.exclusive_min) ?? numberValue(external.exclusiveMinimum),
    exclusive_max: numberValue(external.exclusive_max) ?? numberValue(external.exclusiveMaximum),
    step: numberValue(external.step) ?? numberValue(node.step),
    unit: typeof external.unit === 'string' ? external.unit : typeof node.unit === 'string' ? node.unit : undefined,
    required: typeof external.required === 'boolean' ? external.required : undefined,
    pattern: typeof external.pattern === 'string' ? external.pattern : undefined,
  };
}

function normalizeStaticOptions(node: MemoryConfigSchemaNode) {
  const source = node.options ?? node.enum ?? [];
  if (Array.isArray(source)) {
    return source.flatMap((option) => {
      if (typeof option === 'string' || typeof option === 'number' || typeof option === 'boolean') {
        return [{ value: String(option), label: String(option) }];
      }
      if (!isObject(option)) return [];
      const rawValue = option.value ?? option.id ?? option.key;
      if (typeof rawValue !== 'string' && typeof rawValue !== 'number' && typeof rawValue !== 'boolean') return [];
      return [{ value: String(rawValue), label: String(option.label ?? option.name ?? rawValue) }];
    });
  }
  if (isObject(source)) {
    return Object.entries(source).map(([value, label]) => ({ value, label: typeof label === 'string' ? label : value }));
  }
  return [];
}

function providerTypeFor(key: string, node: MemoryConfigSchemaNode): 'llm' | 'embedding' | undefined {
  if (node.provider_type === 'llm' || node.provider_type === 'embedding') return node.provider_type;
  const normalized = key.toLowerCase();
  if (normalized.includes('embedding') && normalized.includes('provider')) return 'embedding';
  if ((normalized.includes('llm') || normalized.includes('model')) && normalized.includes('provider')) return 'llm';
  if (node._special === 'select_provider') return 'llm';
  return undefined;
}

function providerOptions(
  type: 'llm' | 'embedding',
  providers: MemoryProviderOptions,
  currentValue: unknown,
) {
  const options: Array<{ value: string; label: string; unavailable?: boolean }> = [
    { value: '', label: 'AstrBot 默认' },
    ...providers[type].map((provider) => {
      const detail = [provider.model, provider.type].filter(Boolean).join(' · ');
      return { value: provider.id, label: provider.label ?? (detail || provider.id) };
    }),
  ];
  const current = typeof currentValue === 'string' ? currentValue : '';
  if (current && !options.some((option) => option.value === current)) {
    options.push({ value: current, label: `${current}（当前不可用）`, unavailable: true });
  }
  return options;
}

function makeField(
  key: string,
  path: string[],
  node: MemoryConfigSchemaNode,
  config: MemoryConfigObject,
  providers: MemoryProviderOptions,
  constraints: Record<string, MemoryFieldConstraint | Record<string, unknown>>,
): MemoryConfigField | null {
  const currentValue = getAtPath(config, path);
  const kind = nodeKind(node, currentValue);
  if (!kind) return null;
  const providerType = kind === 'string' ? providerTypeFor(key, node) : undefined;
  return {
    key,
    path,
    kind,
    label: displayLabel(key, node),
    description: (node.label || node.title) && typeof node.description === 'string' ? node.description : '',
    hint: typeof node.hint === 'string' ? node.hint : '',
    defaultValue: node.default,
    options: providerType ? providerOptions(providerType, providers, currentValue) : normalizeStaticOptions(node),
    constraint: normalizeConstraint(node, constraints, path),
    providerType,
  };
}

function collectObjectCategories(
  properties: Record<string, MemoryConfigSchemaNode>,
  parentPath: string[],
  config: MemoryConfigObject,
  providers: MemoryProviderOptions,
  constraints: Record<string, MemoryFieldConstraint | Record<string, unknown>>,
  categories: MemoryConfigCategory[],
) {
  for (const [key, node] of Object.entries(properties)) {
    const path = [...parentPath, key];
    const children = childProperties(node);
    if (!children) continue;
    const fields = Object.entries(children).flatMap(([childKey, childNode]) => {
      const field = makeField(childKey, [...path, childKey], childNode, config, providers, constraints);
      return field ? [field] : [];
    });
    if (fields.length) categories.push({ key: pathKey(path), title: displayLabel(key, node), fields });
    collectObjectCategories(children, path, config, providers, constraints, categories);
  }
}

function consolidateCategories(categories: readonly MemoryConfigCategory[]): MemoryConfigCategory[] {
  const grouped = new Map<string, MemoryConfigCategory>();
  for (const category of categories) {
    const source = category.key.split('.')[0] ?? category.key;
    const group = CATEGORY_GROUP_BY_SOURCE.get(source) ?? OTHER_CATEGORY_GROUP;
    const current = grouped.get(group.key);
    if (current) current.fields.push(...category.fields);
    else grouped.set(group.key, { key: group.key, title: group.title, fields: [...category.fields] });
  }
  return [...CATEGORY_GROUPS, OTHER_CATEGORY_GROUP].flatMap((group) => {
    const category = grouped.get(group.key);
    return category ? [category] : [];
  });
}

export function parseMemoryConfigSchema(
  schema: MemoryConfigSchemaNode | Record<string, MemoryConfigSchemaNode>,
  config: MemoryConfigObject,
  providers: MemoryProviderOptions,
  constraints: Record<string, MemoryFieldConstraint | Record<string, unknown>>,
): ParsedMemoryConfigSchema {
  const properties = schemaProperties(schema, config);
  const basicFields = Object.entries(properties).flatMap(([key, node]) => {
    if (childProperties(node) || node.type === 'object') return [];
    const field = makeField(key, [key], node, config, providers, constraints);
    return field ? [field] : [];
  });
  const schemaCategories: MemoryConfigCategory[] = basicFields.length
    ? [{ key: 'basic', title: '基础设置', fields: basicFields }]
    : [];
  collectObjectCategories(properties, [], config, providers, constraints, schemaCategories);
  const categories = consolidateCategories(schemaCategories);
  return { categories, fields: categories.flatMap((category) => category.fields) };
}

export function createMemoryConfigDraft(config: MemoryConfigObject, fields: readonly MemoryConfigField[]): MemoryConfigDraft {
  let draft = deepClone(config);
  for (const field of fields) {
    if (field.kind !== 'integer' && field.kind !== 'number') continue;
    const current = getAtPath(draft, field.path);
    if (current !== undefined && current !== null) draft = setAtPath(draft, field.path, String(current));
  }
  return draft;
}

function valuesEqual(left: unknown, right: unknown): boolean {
  if (typeof left === 'number' || typeof right === 'number') return String(left) === String(right);
  return JSON.stringify(left) === JSON.stringify(right);
}

export function countMemoryConfigChanges(
  draft: MemoryConfigDraft,
  original: MemoryConfigObject,
  fields: readonly MemoryConfigField[],
): number {
  return fields.reduce((count, field) => (
    count + (valuesEqual(getAtPath(draft, field.path), getAtPath(original, field.path)) ? 0 : 1)
  ), 0);
}

function parseStrictNumber(raw: unknown, integer: boolean): number | null {
  if (typeof raw === 'number') return Number.isFinite(raw) && (!integer || Number.isInteger(raw)) ? raw : null;
  if (typeof raw !== 'string' || raw.trim() !== raw || raw === '') return null;
  const pattern = integer ? /^[+-]?(?:0|[1-9]\d*)$/ : /^[+-]?(?:(?:\d+\.?\d*)|(?:\.\d+))(?:[eE][+-]?\d+)?$/;
  if (!pattern.test(raw)) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && (!integer || Number.isInteger(parsed)) ? parsed : null;
}

export function validateMemoryConfigDraft(
  draft: MemoryConfigDraft,
  fields: readonly MemoryConfigField[],
): MemoryValidationErrors {
  const errors: MemoryValidationErrors = {};
  for (const field of fields) {
    const value = getAtPath(draft, field.path);
    const key = pathKey(field.path);
    if (field.constraint.required && (value === '' || value === null || value === undefined)) {
      errors[key] = '此字段不能为空。';
      continue;
    }
    if (field.kind === 'integer' || field.kind === 'number') {
      const parsed = parseStrictNumber(value, field.kind === 'integer');
      if (parsed === null) {
        errors[key] = field.kind === 'integer' ? '请输入有效整数。' : '请输入有效数字。';
        continue;
      }
      const { min, max, exclusive_min: exclusiveMin, exclusive_max: exclusiveMax, step } = field.constraint;
      if (min !== undefined && parsed < min) errors[key] = `不能小于 ${min}。`;
      else if (max !== undefined && parsed > max) errors[key] = `不能大于 ${max}。`;
      else if (exclusiveMin !== undefined && parsed <= exclusiveMin) errors[key] = `必须大于 ${exclusiveMin}。`;
      else if (exclusiveMax !== undefined && parsed >= exclusiveMax) errors[key] = `必须小于 ${exclusiveMax}。`;
      else if (step !== undefined && step > 0) {
        const offset = (parsed - (min ?? 0)) / step;
        if (Math.abs(offset - Math.round(offset)) > 1e-8) errors[key] = `必须按 ${step} 递增。`;
      }
      continue;
    }
    if (field.kind === 'string' && field.constraint.pattern && typeof value === 'string') {
      try {
        if (!new RegExp(field.constraint.pattern).test(value)) errors[key] = '输入格式不符合要求。';
      } catch {
        // Ignore invalid backend patterns rather than blocking all saves.
      }
    }
  }
  return errors;
}

export function convertMemoryConfigDraft(
  draft: MemoryConfigDraft,
  fields: readonly MemoryConfigField[],
): MemoryConfigObject {
  const errors = validateMemoryConfigDraft(draft, fields);
  if (Object.keys(errors).length) throw new Error('配置字段校验未通过');
  let converted = deepClone(draft);
  for (const field of fields) {
    if (field.kind !== 'integer' && field.kind !== 'number') continue;
    const parsed = parseStrictNumber(getAtPath(draft, field.path), field.kind === 'integer');
    if (parsed === null) throw new Error(`无法转换字段 ${pathKey(field.path)}`);
    converted = setAtPath(converted, field.path, parsed);
  }
  return converted;
}

export function memoryConfigEquals(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => memoryConfigEquals(value, right[index]));
  }
  if (!isObject(left) || !isObject(right)) return false;
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key, index) => key === rightKeys[index]
      && memoryConfigEquals(left[key], right[key]));
}
