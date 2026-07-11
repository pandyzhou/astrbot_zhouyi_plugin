import type { ReactNode } from 'react';

export type DataStateKind = 'loading' | 'empty' | 'error';

export interface DataStateProps {
  state: DataStateKind;
  title: string;
  message?: string;
  action?: ReactNode;
}

export function DataState({ state, title, message, action }: DataStateProps) {
  return (
    <div className={`wf-data-state wf-data-state--${state}`} role={state === 'error' ? 'alert' : 'status'}>
      <strong>{title}</strong>
      {message ? <p>{message}</p> : null}
      {action ? <div className="wf-data-state-action">{action}</div> : null}
    </div>
  );
}
