import type { ReactNode } from 'react';

export interface WorkshopShellProps {
  brand: string;
  navigation: ReactNode;
  groupControl?: ReactNode;
  children: ReactNode;
}

export function WorkshopShell({ brand, navigation, groupControl, children }: WorkshopShellProps) {
  return (
    <div className="wf-shell">
      <a className="wf-skip-link" href="#main-content">
        跳到主要内容
      </a>
      <header className="wf-topbar">
        <div className="wf-brand" aria-label={brand}>
          <span className="wf-brand-mark" aria-hidden="true">MC</span>
          <span>{brand}</span>
        </div>
        <nav className="wf-nav" aria-label="主导航">
          {navigation}
        </nav>
        {groupControl ? <div className="wf-group-control">{groupControl}</div> : null}
      </header>
      <main className="wf-main" id="main-content">
        {children}
      </main>
    </div>
  );
}
