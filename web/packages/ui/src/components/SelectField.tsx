import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';

export interface SelectFieldOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectFieldProps {
  id: string;
  label: string;
  options: SelectFieldOption[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  className?: string;
  placeholder?: string;
}

const CLOSE_ANIMATION_MS = 180;
const TYPEAHEAD_RESET_MS = 500;

function findEnabledIndex(
  options: SelectFieldOption[],
  startIndex: number,
  direction: 1 | -1,
) {
  if (options.length === 0) return -1;

  for (let offset = 1; offset <= options.length; offset += 1) {
    const index = (startIndex + direction * offset + options.length) % options.length;
    if (!options[index]?.disabled) return index;
  }

  return -1;
}

export function SelectField({
  id,
  label,
  options,
  value,
  onChange,
  disabled = false,
  className,
  placeholder = '请选择',
}: SelectFieldProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const optionRefs = useRef<Array<HTMLLIElement | null>>([]);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const typeaheadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const typeaheadRef = useRef('');
  const [open, setOpen] = useState(false);
  const [listboxMounted, setListboxMounted] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);

  const labelId = `${id}-label`;
  const listboxId = `${id}-listbox`;
  const selectedIndex = useMemo(
    () => options.findIndex((option) => option.value === value),
    [options, value],
  );
  const selectedOption = selectedIndex >= 0 ? options[selectedIndex] : undefined;

  const firstEnabledIndex = useMemo(
    () => options.findIndex((option) => !option.disabled),
    [options],
  );

  const lastEnabledIndex = useMemo(() => {
    for (let index = options.length - 1; index >= 0; index -= 1) {
      if (!options[index]?.disabled) return index;
    }
    return -1;
  }, [options]);

  const initialActiveIndex = selectedOption?.disabled
    ? firstEnabledIndex
    : selectedIndex >= 0
      ? selectedIndex
      : firstEnabledIndex;

  const clearCloseTimer = useCallback(() => {
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }, []);

  const openListbox = useCallback((nextActiveIndex = initialActiveIndex) => {
    if (disabled || nextActiveIndex < 0) return;
    clearCloseTimer();
    setActiveIndex(nextActiveIndex);
    setListboxMounted(true);
    setOpen(true);
  }, [clearCloseTimer, disabled, initialActiveIndex]);

  const closeListbox = useCallback(() => {
    clearCloseTimer();
    setOpen(false);
    closeTimerRef.current = setTimeout(() => {
      setListboxMounted(false);
      closeTimerRef.current = null;
    }, CLOSE_ANIMATION_MS);
  }, [clearCloseTimer]);

  const chooseOption = useCallback((index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    if (option.value !== value) onChange(option.value);
    closeListbox();
  }, [closeListbox, onChange, options, value]);

  useEffect(() => {
    if (!open) return undefined;

    const handleOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeListbox();
    };

    document.addEventListener('pointerdown', handleOutsidePointer);
    return () => document.removeEventListener('pointerdown', handleOutsidePointer);
  }, [closeListbox, open]);

  useEffect(() => {
    if (open && activeIndex >= 0) {
      optionRefs.current[activeIndex]?.scrollIntoView({ block: 'nearest' });
    }
  }, [activeIndex, open]);

  useEffect(() => {
    if (disabled && (open || listboxMounted)) closeListbox();
  }, [closeListbox, disabled, listboxMounted, open]);

  useEffect(() => () => {
    clearCloseTimer();
    if (typeaheadTimerRef.current) clearTimeout(typeaheadTimerRef.current);
  }, [clearCloseTimer]);

  function moveActive(direction: 1 | -1) {
    const startIndex = activeIndex >= 0
      ? activeIndex
      : direction === 1
        ? options.length - 1
        : 0;
    const nextIndex = findEnabledIndex(options, startIndex, direction);
    if (nextIndex >= 0) setActiveIndex(nextIndex);
  }

  function handleTypeahead(character: string) {
    if (typeaheadTimerRef.current) clearTimeout(typeaheadTimerRef.current);
    typeaheadRef.current += character.toLocaleLowerCase();
    typeaheadTimerRef.current = setTimeout(() => {
      typeaheadRef.current = '';
      typeaheadTimerRef.current = null;
    }, TYPEAHEAD_RESET_MS);

    const query = typeaheadRef.current;
    const searchStart = open && activeIndex >= 0 ? activeIndex : -1;
    for (let offset = 1; offset <= options.length; offset += 1) {
      const index = (searchStart + offset) % options.length;
      const option = options[index];
      if (!option?.disabled && option.label.toLocaleLowerCase().startsWith(query)) {
        if (open) setActiveIndex(index);
        else openListbox(index);
        return;
      }
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (disabled) return;

    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (open) moveActive(1);
        else openListbox();
        return;
      case 'ArrowUp':
        event.preventDefault();
        if (open) moveActive(-1);
        else openListbox();
        return;
      case 'Home':
        event.preventDefault();
        if (open) setActiveIndex(firstEnabledIndex);
        else openListbox(firstEnabledIndex);
        return;
      case 'End':
        event.preventDefault();
        if (open) setActiveIndex(lastEnabledIndex);
        else openListbox(lastEnabledIndex);
        return;
      case 'Enter':
      case ' ':
        event.preventDefault();
        if (open && activeIndex >= 0) chooseOption(activeIndex);
        else openListbox();
        return;
      case 'Escape':
        if (open) {
          event.preventDefault();
          closeListbox();
        }
        return;
      case 'Tab':
        if (open) closeListbox();
        return;
      default:
        if (event.key.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey) {
          event.preventDefault();
          handleTypeahead(event.key);
        }
    }
  }

  const activeDescendant = open && activeIndex >= 0 ? `${id}-option-${activeIndex}` : undefined;
  const rootClassName = ['wf-select-field', className].filter(Boolean).join(' ');

  return (
    <div className={rootClassName} ref={rootRef}>
      <label className="wf-select-field__label" id={labelId} htmlFor={id}>
        {label}
      </label>
      <div className="wf-select-field__control">
        <button
          className="wf-select-field__trigger"
          id={id}
          type="button"
          role="combobox"
          aria-autocomplete="none"
          aria-controls={listboxId}
          aria-expanded={open}
          aria-haspopup="listbox"
          aria-labelledby={`${labelId} ${id}-value`}
          aria-activedescendant={activeDescendant}
          aria-readonly="true"
          disabled={disabled}
          onClick={() => {
            if (open) closeListbox();
            else openListbox();
          }}
          onKeyDown={handleKeyDown}
        >
          <span
            className={selectedOption ? 'wf-select-field__value' : 'wf-select-field__value wf-select-field__value--placeholder'}
            id={`${id}-value`}
          >
            {selectedOption?.label ?? placeholder}
          </span>
          <span className="wf-select-field__arrow" aria-hidden="true" />
        </button>
        {listboxMounted ? (
          <ul
            className="wf-select-field__listbox"
            id={listboxId}
            role="listbox"
            aria-labelledby={labelId}
            aria-hidden={!open}
            data-state={open ? 'open' : 'closed'}
          >
            {options.map((option, index) => {
              const selected = option.value === value;
              const active = index === activeIndex;
              return (
                <li
                  className="wf-select-field__option"
                  id={`${id}-option-${index}`}
                  key={option.value}
                  role="option"
                  aria-disabled={option.disabled || undefined}
                  aria-selected={selected}
                  data-active={active || undefined}
                  ref={(node) => {
                    optionRefs.current[index] = node;
                  }}
                  onPointerEnter={() => {
                    if (!option.disabled && activeIndex !== index) setActiveIndex(index);
                  }}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => chooseOption(index)}
                >
                  <span className="wf-select-field__marker" aria-hidden="true" />
                  <span>{option.label}</span>
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>
    </div>
  );
}
