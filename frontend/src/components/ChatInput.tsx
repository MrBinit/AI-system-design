import { SendIcon } from "./Icons";
import type { ChatExecutionMode } from "../types";

const MODE_OPTIONS: Array<{ value: ChatExecutionMode; label: string }> = [
  { value: "auto", label: "Auto" },
  { value: "fast", label: "Fast" },
  { value: "deep", label: "Deep" },
];

interface ChatInputProps {
  value: string;
  disabled: boolean;
  mode: ChatExecutionMode;
  onChange: (value: string) => void;
  onModeChange: (mode: ChatExecutionMode) => void;
  onSubmit: () => Promise<void>;
}

export function ChatInput({
  value,
  disabled,
  mode,
  onChange,
  onModeChange,
  onSubmit,
}: ChatInputProps) {
  return (
    <div className="mx-auto w-full max-w-4xl px-4 pb-5">
      <form
        className="flex items-end gap-3 rounded-2xl border border-blue-200 bg-white/95 p-3 shadow-soft backdrop-blur dark:border-slate-700 dark:bg-slate-900"
        onSubmit={async (event) => {
          event.preventDefault();
          await onSubmit();
        }}
      >
        <div className="w-full">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            {MODE_OPTIONS.map((option) => {
              const selected = mode === option.value;
              return (
                <button
                  key={option.value}
                  type="button"
                  disabled={disabled}
                  onClick={() => onModeChange(option.value)}
                  className={[
                    "rounded-lg border px-2.5 py-1 text-xs font-medium transition",
                    selected
                      ? "border-blue-500 bg-blue-50 text-blue-700 dark:border-blue-400 dark:bg-blue-950/40 dark:text-blue-200"
                      : "border-slate-200 text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800",
                    disabled ? "cursor-not-allowed opacity-60" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")}
                  aria-label={`Use ${option.label.toLowerCase()} mode`}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
          <textarea
            value={value}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={async (event) => {
              if (event.key !== "Enter" || event.shiftKey) {
                return;
              }
              event.preventDefault();
              await onSubmit();
            }}
            placeholder="What's in your mind?"
            className="max-h-40 min-h-12 w-full resize-y border-0 bg-transparent p-1.5 text-sm text-slate-800 outline-none placeholder:text-slate-400 disabled:opacity-60 dark:text-slate-100 dark:placeholder:text-slate-500"
          />
        </div>

        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="inline-flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 text-white shadow-lg shadow-blue-500/30 transition hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-45"
          aria-label="Send"
        >
          <SendIcon className="h-4.5 w-4.5" />
        </button>
      </form>
      <p className="mt-2 px-1 text-xs text-slate-500 dark:text-slate-400">Press Enter to send. Shift + Enter for a new line.</p>
    </div>
  );
}
