import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { RecallSession } from './types';
import { buildRecallSessionOptions, formatRecallSessionLabel } from './recallSessions';

function recallSession(overrides: Partial<RecallSession> = {}): RecallSession {
  return {
    session_id: 'aiocqhttp:GroupMessage:123456',
    group_id: '123456',
    display_name: null,
    message_count: 18,
    ...overrides,
  };
}

test('named groups use the group name and number while preserving the complete UMO value', () => {
  const session = recallSession({ display_name: '星光茶话会' });

  assert.deepEqual(buildRecallSessionOptions([session], '群聊'), [{
    value: 'aiocqhttp:GroupMessage:123456',
    label: '星光茶话会（123456）',
  }]);
});

test('missing or identifier-like display names fall back to the group chat label', () => {
  assert.equal(formatRecallSessionLabel(recallSession(), '群聊'), '群聊 123456');
  assert.equal(formatRecallSessionLabel(recallSession({ display_name: '123456' }), '群聊'), '群聊 123456');
  assert.equal(
    formatRecallSessionLabel(recallSession({ display_name: 'aiocqhttp:GroupMessage:123456' }), '群聊'),
    '群聊 123456',
  );
  assert.equal(formatRecallSessionLabel(recallSession({ display_name: '   ' }), '群聊'), '群聊 123456');
});

test('options preserve backend order and never append message counts', () => {
  const sessions = [
    recallSession({ session_id: 'platform:GroupMessage:200', group_id: '200', display_name: '第二群', message_count: 99 }),
    recallSession({ session_id: 'platform:GroupMessage:100', group_id: '100', display_name: null, message_count: 1 }),
  ];

  assert.deepEqual(buildRecallSessionOptions(sessions, '群聊'), [
    { value: 'platform:GroupMessage:200', label: '第二群（200）' },
    { value: 'platform:GroupMessage:100', label: '群聊 100' },
  ]);
});

test('invalid sessions without a usable session or group identifier are omitted', () => {
  const sessions = [
    recallSession({ session_id: ' ' }),
    recallSession({ group_id: '' }),
    recallSession({ session_id: 'platform:GroupMessage:300', group_id: '300' }),
  ];

  assert.deepEqual(buildRecallSessionOptions(sessions, '群聊'), [
    { value: 'platform:GroupMessage:300', label: '群聊 300' },
  ]);
});
