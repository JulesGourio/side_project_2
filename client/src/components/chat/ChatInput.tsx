import { useEffect, useRef } from 'react';
import { Send, Square } from 'lucide-react';

interface ChatInputProps {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop?: () => void;
  disabled: boolean;
  streaming: boolean;
  placeholder?: string;
}

export function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  disabled,
  streaming,
  placeholder = 'Ask a question about your documents…',
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    const lineHeight = 24;
    const maxRows = 6;
    el.style.height = `${Math.min(el.scrollHeight, maxRows * lineHeight + 20)}px`;
  }, [value]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (!disabled && value.trim()) onSend();
    }
  };

  const canSend = !disabled && value.trim().length > 0;

  return (
    <div
      className="px-4 py-3 border-t"
      style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-primary)' }}
    >
      <div
        className="flex items-end gap-2 rounded-2xl border px-3 py-2 transition-colors"
        style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-secondary)' }}
      >
        <textarea
          ref={textareaRef}
          value={value}
          onChange={e => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled && !streaming}
          placeholder={placeholder}
          rows={1}
          className="flex-1 resize-none bg-transparent outline-none text-sm leading-6 py-1"
          style={{ color: 'var(--color-text-primary)' }}
        />

        {streaming ? (
          <button
            onClick={onStop}
            className="flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center transition-all cursor-pointer"
            style={{ background: 'var(--color-error)', color: '#fff' }}
            title="Stop generation"
          >
            <Square className="h-3.5 w-3.5" fill="currentColor" />
          </button>
        ) : (
          <button
            onClick={() => canSend && onSend()}
            disabled={!canSend}
            className="flex-shrink-0 w-8 h-8 rounded-xl flex items-center justify-center transition-all cursor-pointer disabled:opacity-40 disabled:cursor-default"
            style={{
              background: canSend
                ? 'linear-gradient(135deg, var(--color-accent-primary) 0%, var(--color-accent-secondary) 100%)'
                : 'var(--color-bg-tertiary)',
              color: canSend ? '#fff' : 'var(--color-text-muted)',
            }}
            title="Send (Enter)"
          >
            <Send className="h-3.5 w-3.5" />
          </button>
        )}
      </div>

      <p className="mt-1.5 text-center text-xs" style={{ color: 'var(--color-text-muted)' }}>
        Enter to send · Shift+Enter for new line
      </p>
    </div>
  );
}
