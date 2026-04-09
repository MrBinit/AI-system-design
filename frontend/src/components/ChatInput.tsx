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
  suggestionChips: string[];
  autoScrollPaused: boolean;
  onChange: (value: string) => void;
  onModeChange: (mode: ChatExecutionMode) => void;
  onSuggestionClick: (value: string) => Promise<void>;
  onToggleAutoScroll: () => void;
  onSubmit: () => Promise<void>;
}

export function ChatInput({
  value,
  disabled,
  mode,
  suggestionChips,
  autoScrollPaused,
  onChange,
  onModeChange,
  onSuggestionClick,
  onToggleAutoScroll,
  onSubmit,
}: ChatInputProps) {
  const modeHint =
    mode === "fast"
      ? "Fast: quickest answer with minimal extra passes."
      : mode === "deep"
        ? "Deep: slower, higher coverage and verification."
        : "Auto: balanced speed and depth based on question complexity.";

  return (
    <div className="mx-auto w-full max-w-4xl px-4 pb-5">
      {suggestionChips.length ? (
        <div className="mb-2 flex flex-wrap gap-1.5 px-1">
          {suggestionChips.map((chip) => (
            <button
              key={chip}
              type="button"
              disabled={disabled}
              onClick={async () => {
                await onSuggestionClick(chip);
              }}
              className="rounded-full border border-blue-200 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-60 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              {chip}
            </button>
          ))}
        </div>
      ) : null}
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
          <p className="mb-2 text-[11px] text-slate-500 dark:text-slate-400">{modeHint}</p>
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
      <div className="mt-2 flex items-center justify-between px-1 text-xs text-slate-500 dark:text-slate-400">
        <p>Press Enter to send. Shift + Enter for a new line. Slash: /fast /deep /auto /cite /summarize /web</p>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onToggleAutoScroll}
            className="rounded border border-slate-200 px-2 py-0.5 text-[11px] hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            {autoScrollPaused ? "Resume auto-scroll" : "Pause auto-scroll"}
          </button>
          <span className={value.length > 5000 ? "font-semibold text-rose-500" : ""}>{value.length} chars</span>
        </div>
      </div>
    </div>
  );
}
