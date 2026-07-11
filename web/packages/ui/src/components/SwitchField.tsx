import type { ChangeEvent, ReactNode } from 'react';

export interface SwitchFieldProps {
  id: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  description?: ReactNode;
  disabled?: boolean;
  className?: string;
}

export function SwitchField({
  id,
  label,
  checked,
  onChange,
  description,
  disabled = false,
  className,
}: SwitchFieldProps) {
  const descriptionId = description ? `${id}-description` : undefined;
  const rootClassName = ['wf-switch-field', className].filter(Boolean).join(' ');

  function handleChange(event: ChangeEvent<HTMLInputElement>) {
    onChange(event.target.checked);
  }

  return (
    <div className={rootClassName}>
      <div className="wf-switch-field__copy">
        <label className="wf-switch-field__label" htmlFor={id}>{label}</label>
        {description ? <div className="wf-switch-field__description" id={descriptionId}>{description}</div> : null}
      </div>
      <label className="wf-switch-field__control" htmlFor={id}>
        <input
          className="wf-switch-field__input"
          id={id}
          type="checkbox"
          role="switch"
          checked={checked}
          disabled={disabled}
          aria-describedby={descriptionId}
          onChange={handleChange}
        />
        <span className="wf-switch-field__track" aria-hidden="true">
          <span className="wf-switch-field__thumb" />
        </span>
        <span className="wf-sr-only">{checked ? '已开启' : '已关闭'}</span>
      </label>
    </div>
  );
}
