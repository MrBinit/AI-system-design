import { DislikeIcon, LikeIcon, SparklesIcon } from "./Icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage, ReactionType } from "../types";

interface MessageCardProps {
  message: ChatMessage;
  onRegenerate: (sourcePrompt: string) => void;
  onReaction: (messageId: string, reaction: ReactionType) => void;
}

function avatarForRole(role: ChatMessage["role"]): string {
  return role === "assistant" ? "AI" : "ME";
}

function cardClass(role: ChatMessage["role"]): string {
  if (role === "assistant") {
    return "border border-blue-100 bg-white shadow-soft dark:border-slate-700 dark:bg-slate-900";
  }
  return "border border-blue-100 bg-gradient-to-br from-blue-50/80 to-white shadow-sm dark:border-slate-700 dark:bg-slate-800/80";
}

export function MessageCard({ message, onRegenerate, onReaction }: MessageCardProps) {
  return (
    <article className="animate-[fade-up_240ms_ease-out] space-y-2">
      <div className="flex items-start gap-3">
        <div className="mt-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-blue-500 to-indigo-600 text-xs font-bold text-white">
          {avatarForRole(message.role)}
        </div>

        <div className={`w-full rounded-2xl p-4 ${cardClass(message.role)}`}>
          <div className="mb-2 flex items-center justify-between">
            <div>
              <p className="text-sm font-semibold text-slate-900 dark:text-slate-100">{message.username}</p>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {new Date(message.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
              </p>
            </div>
            {message.role === "assistant" ? (
              <span className="rounded-full bg-blue-100 px-2 py-1 text-[11px] font-medium text-blue-700 dark:bg-slate-700 dark:text-slate-200">
                AI response
              </span>
            ) : null}
          </div>

          {message.role === "assistant" ? (
            <div className="text-[15px] leading-7 text-slate-700 dark:text-slate-200">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  h1: ({ children }) => (
                    <h1 className="mt-1 mb-3 text-2xl font-semibold text-slate-900 dark:text-slate-100">
                      {children}
                    </h1>
                  ),
                  h2: ({ children }) => (
                    <h2 className="mt-4 mb-2 text-xl font-semibold text-slate-900 dark:text-slate-100">
                      {children}
                    </h2>
                  ),
                  h3: ({ children }) => (
                    <h3 className="mt-3 mb-2 text-lg font-semibold text-slate-900 dark:text-slate-100">
                      {children}
                    </h3>
                  ),
                  p: ({ children }) => <p className="my-3 whitespace-pre-wrap">{children}</p>,
                  strong: ({ children }) => <strong className="font-semibold text-slate-900 dark:text-slate-100">{children}</strong>,
                  ul: ({ children }) => <ul className="my-3 list-disc space-y-1 pl-6">{children}</ul>,
                  ol: ({ children }) => <ol className="my-3 list-decimal space-y-1 pl-6">{children}</ol>,
                  li: ({ children }) => <li>{children}</li>,
                  hr: () => <hr className="my-5 border-blue-100 dark:border-slate-700" />,
                  code: ({ children }) => (
                    <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[13px] text-slate-800 dark:bg-slate-800 dark:text-slate-100">
                      {children}
                    </code>
                  ),
                  pre: ({ children }) => (
                    <pre className="my-4 overflow-x-auto rounded-xl bg-slate-950 p-3 text-[13px] text-slate-100">
                      {children}
                    </pre>
                  ),
                  table: ({ children }) => (
                    <div className="my-4 overflow-x-auto">
                      <table className="min-w-full border-collapse text-left text-sm">{children}</table>
                    </div>
                  ),
                  thead: ({ children }) => (
                    <thead className="bg-blue-50/70 dark:bg-slate-800/80">{children}</thead>
                  ),
                  th: ({ children }) => (
                    <th className="border border-blue-100 px-3 py-2 font-semibold text-slate-900 dark:border-slate-700 dark:text-slate-100">
                      {children}
                    </th>
                  ),
                  td: ({ children }) => (
                    <td className="border border-blue-100 px-3 py-2 align-top dark:border-slate-700">
                      {children}
                    </td>
                  ),
                }}
              >
                {message.content}
              </ReactMarkdown>
            </div>
          ) : (
            <p className="whitespace-pre-wrap text-[15px] leading-7 text-slate-700 dark:text-slate-200">
              {message.content}
            </p>
          )}

          {message.role === "assistant" && message.sourcePrompt ? (
            <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-blue-100 pt-3 dark:border-slate-700">
              <button
                type="button"
                onClick={() => onRegenerate(message.sourcePrompt || "")}
                className="inline-flex items-center gap-1 rounded-lg border border-blue-200 bg-blue-50 px-3 py-1.5 text-xs font-medium text-blue-700 transition hover:bg-blue-100 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
              >
                <SparklesIcon className="h-3.5 w-3.5" />
                Regenerate
              </button>

              <button
                type="button"
                onClick={() => onReaction(message.id, message.reaction === "like" ? null : "like")}
                className={`rounded-lg border px-2.5 py-1.5 text-xs transition ${
                  message.reaction === "like"
                    ? "border-blue-500 bg-blue-600 text-white"
                    : "border-slate-200 bg-white text-slate-600 hover:border-blue-200 hover:text-blue-600 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
                }`}
              >
                <LikeIcon className="h-3.5 w-3.5" />
              </button>

              <button
                type="button"
                onClick={() => onReaction(message.id, message.reaction === "dislike" ? null : "dislike")}
                className={`rounded-lg border px-2.5 py-1.5 text-xs transition ${
                  message.reaction === "dislike"
                    ? "border-rose-500 bg-rose-600 text-white"
                    : "border-slate-200 bg-white text-slate-600 hover:border-rose-200 hover:text-rose-600 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-200"
                }`}
              >
                <DislikeIcon className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}
