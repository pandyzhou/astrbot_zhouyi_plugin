import type { HTMLAttributes, ReactNode } from 'react';

export interface WorkshopPanelProps extends HTMLAttributes<HTMLElement> {
  title?: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function WorkshopPanel({ title, description, actions, children, className = '', ...props }: WorkshopPanelProps) {
  return (
    <section className={`wf-panel ${className}`.trim()} {...props}>
      {title || description || actions ? (
        <header className="wf-panel-heading">
          <div>
            {title ? <h2>{title}</h2> : null}
            {description ? <p>{description}</p> : null}
          </div>
          {actions ? <div className="wf-panel-actions">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}
