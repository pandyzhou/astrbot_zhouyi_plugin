import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { test } from 'node:test';
import type { MemoryConfigObject, MemoryConfigSchemaNode } from '../../api/types';
import {
  convertMemoryConfigDraft,
  countMemoryConfigChanges,
  createMemoryConfigDraft,
  getAtPath,
  memoryConfigEquals,
  parseMemoryConfigSchema,
  setAtPath,
  validateMemoryConfigDraft,
} from './memoryConfigSchema';

const schema: MemoryConfigSchemaNode = {
  type: 'object',
  properties: {
    enabled: { type: 'boolean', description: '启用记忆' },
    bot_language: { type: 'string', options: ['zh', 'en'], default: 'zh' },
    retrieval: {
      type: 'object',
      title: '检索设置',
      properties: {
        top_k: { type: 'integer', default: 5 },
        threshold: { type: 'number', default: 0.6 },
        llm_provider_id: { type: 'string', provider_type: 'llm' },
      },
    },
  },
};

const config: MemoryConfigObject = {
  enabled: true,
  bot_language: 'zh',
  retrieval: { top_k: 5, threshold: 0.6, llm_provider_id: 'missing-provider' },
};

function parsed() {
  return parseMemoryConfigSchema(
    schema,
    config,
    { llm: [{ id: 'provider-a', label: 'Provider A' }], embedding: [] },
    { 'retrieval.top_k': { min: 1, max: 20, step: 1 }, retrieval: { threshold: { exclusive_min: 0, max: 1, step: 0.1 } } },
  );
}

test('解析顶层基础设置、对象分类和 provider 当前不可用选项', () => {
  const result = parsed();
  assert.deepEqual(result.categories.map((category) => category.title), ['基础与模型', '会话与召回']);
  assert.deepEqual(result.categories[0]?.fields.map((field) => field.key), ['enabled', 'bot_language']);
  const provider = result.fields.find((field) => field.key === 'llm_provider_id');
  assert.equal(provider?.options[0]?.value, '');
  assert.equal(provider?.options.at(-1)?.value, 'missing-provider');
  assert.equal(provider?.options.at(-1)?.unavailable, true);
});

test('兼容 AstrBot items、bool 和外层 memory 包装格式', () => {
  const astrbotSchema: Record<string, MemoryConfigSchemaNode> = {
    memory: {
      type: 'object',
      items: {
        enabled: { type: 'bool', description: '启用长期记忆', default: true },
        retrieval: {
          type: 'object',
          description: '检索设置',
          items: { top_k: { type: 'int', description: '召回数量', default: 5 } },
        },
      },
    },
  };
  const result = parseMemoryConfigSchema(astrbotSchema, { enabled: true, retrieval: { top_k: 5 } }, { llm: [], embedding: [] }, {});
  assert.deepEqual(result.categories.map((category) => category.title), ['基础与模型', '会话与召回']);
  assert.equal(result.fields.find((field) => field.key === 'enabled')?.kind, 'boolean');
  assert.equal(result.fields.find((field) => field.key === 'top_k')?.kind, 'integer');
});

test('按业务语义收敛为五个主要分类，并将未知区段归入其他设置', () => {
  const groupedSchema: MemoryConfigSchemaNode = {
    type: 'object',
    properties: {
      enabled: { type: 'boolean' },
      provider_settings: { type: 'object', properties: { llm_provider_id: { type: 'string' } } },
      session_manager: { type: 'object', properties: { max_sessions: { type: 'integer' } } },
      reflection_engine: { type: 'object', properties: { summary_trigger_rounds: { type: 'integer' } } },
      graph_memory: { type: 'object', properties: { enabled: { type: 'boolean' } } },
      backup_settings: { type: 'object', properties: { enabled: { type: 'boolean' } } },
      future_section: { type: 'object', properties: { enabled: { type: 'boolean' } } },
    },
  };
  const groupedConfig: MemoryConfigObject = {
    enabled: true,
    provider_settings: { llm_provider_id: '' },
    session_manager: { max_sessions: 100 },
    reflection_engine: { summary_trigger_rounds: 10 },
    graph_memory: { enabled: true },
    backup_settings: { enabled: true },
    future_section: { enabled: true },
  };
  const result = parseMemoryConfigSchema(groupedSchema, groupedConfig, { llm: [], embedding: [] }, {});

  assert.deepEqual(result.categories.map((category) => category.title), [
    '基础与模型',
    '会话与召回',
    '记忆处理',
    '图记忆与权重',
    '数据维护',
    '其他设置',
  ]);
});

test('完整 AstrBot Memory Schema 只生成五个主要分类', () => {
  const fullSchema = JSON.parse(
    readFileSync(resolve(process.cwd(), '../../../_conf_schema.json'), 'utf8'),
  ) as Record<string, MemoryConfigSchemaNode>;
  const result = parseMemoryConfigSchema(fullSchema.memory ?? fullSchema, {}, { llm: [], embedding: [] }, {});

  assert.deepEqual(result.categories.map((category) => category.title), [
    '基础与模型',
    '会话与召回',
    '记忆处理',
    '图记忆与权重',
    '数据维护',
  ]);
  assert.ok(result.fields.length > 30);
  const injectionMethod = result.fields.find((field) => field.path.join('.') === 'recall_engine.injection_method');
  assert.deepEqual(injectionMethod?.options.map((option) => option.value), [
    'extra_user_content',
    'user_message_before',
    'user_message_after',
    'fake_tool_call',
  ]);
  assert.doesNotMatch(injectionMethod?.hint ?? '', /已废弃|fake_tool_call_deepseek_v4|system_prompt/);
});

test('路径 set 为不可变更新且数字 draft 保留字符串', () => {
  const result = parsed();
  const draft = createMemoryConfigDraft(config, result.fields);
  assert.equal(getAtPath(draft, ['retrieval', 'top_k']), '5');
  const changed = setAtPath(draft, ['retrieval', 'top_k'], '8');
  assert.equal(getAtPath(draft, ['retrieval', 'top_k']), '5');
  assert.equal(getAtPath(changed, ['retrieval', 'top_k']), '8');
  assert.equal(countMemoryConfigChanges(changed, config, result.fields), 1);
});

test('严格转换整数和浮点数并保留未呈现配置', () => {
  const result = parsed();
  let draft = createMemoryConfigDraft({ ...config, opaque: { keep: 'yes' } }, result.fields);
  draft = setAtPath(draft, ['retrieval', 'top_k'], '12');
  draft = setAtPath(draft, ['retrieval', 'threshold'], '.7');
  const converted = convertMemoryConfigDraft(draft, result.fields);
  assert.equal(getAtPath(converted, ['retrieval', 'top_k']), 12);
  assert.equal(getAtPath(converted, ['retrieval', 'threshold']), 0.7);
  assert.deepEqual(converted.opaque, { keep: 'yes' });
});

test('字段校验拒绝空值、非整数、越界和 step 不匹配', () => {
  const result = parsed();
  let draft = createMemoryConfigDraft(config, result.fields);
  draft = setAtPath(draft, ['retrieval', 'top_k'], '1.5');
  draft = setAtPath(draft, ['retrieval', 'threshold'], '0.65');
  let errors = validateMemoryConfigDraft(draft, result.fields);
  assert.equal(errors['retrieval.top_k'], '请输入有效整数。');
  assert.equal(errors['retrieval.threshold'], '必须按 0.1 递增。');

  draft = setAtPath(draft, ['retrieval', 'top_k'], '21');
  draft = setAtPath(draft, ['retrieval', 'threshold'], '0');
  errors = validateMemoryConfigDraft(draft, result.fields);
  assert.equal(errors['retrieval.top_k'], '不能大于 20。');
  assert.equal(errors['retrieval.threshold'], '必须大于 0。');
});

test('深比较忽略对象 key 顺序但保留数组顺序语义', () => {
  const left = {
    enabled: true,
    provider_settings: { llm_provider_id: 'llm-main', embedding_provider_id: 'embedding-main' },
    recall_engine: { top_k: 5, weights: [1, { importance: 0.8, similarity: 0.2 }] },
  };
  const reordered = {
    recall_engine: { weights: [1, { similarity: 0.2, importance: 0.8 }], top_k: 5 },
    provider_settings: { embedding_provider_id: 'embedding-main', llm_provider_id: 'llm-main' },
    enabled: true,
  };
  assert.equal(memoryConfigEquals(left, reordered), true);
  assert.equal(memoryConfigEquals(left, { ...reordered, recall_engine: { ...reordered.recall_engine, top_k: 6 } }), false);
  assert.equal(memoryConfigEquals([1, 2], [2, 1]), false);
});
