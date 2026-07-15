import type { RecallSession } from './types';

export interface RecallSessionOption {
  value: string;
  label: string;
}

function normalized(value: string | null | undefined): string {
  return value?.trim() ?? '';
}

export function formatRecallSessionLabel(session: RecallSession, groupChatLabel: string): string {
  const groupId = normalized(session.group_id);
  const sessionId = normalized(session.session_id);
  const displayName = normalized(session.display_name);
  if (displayName && displayName !== groupId && displayName !== sessionId) {
    return `${displayName}（${groupId}）`;
  }
  return `${groupChatLabel} ${groupId}`;
}

export function buildRecallSessionOptions(
  sessions: readonly RecallSession[] | null | undefined,
  groupChatLabel: string,
): RecallSessionOption[] {
  return (sessions ?? []).flatMap((session) => {
    if (!normalized(session.session_id) || !normalized(session.group_id)) return [];
    return [{
      value: session.session_id,
      label: formatRecallSessionLabel(session, groupChatLabel),
    }];
  });
}
