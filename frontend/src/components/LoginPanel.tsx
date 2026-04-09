import { useState } from "react";

import { SparklesIcon } from "./Icons";

interface LoginPanelProps {
  loading: boolean;
  error: string;
  onSubmit: (username: string, password: string, sessionId: string) => Promise<void>;
}

export function LoginPanel({ loading, error, onSubmit }: LoginPanelProps) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin");
  const [sessionId, setSessionId] = useState("");

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-app-gradient px-4 dark:bg-app-gradient-dark">
      <div className="pointer-events-none absolute -top-40 right-[-80px] h-80 w-80 rounded-full bg-blue-200/60 blur-3xl dark:bg-blue-900/30" />
      <div className="pointer-events-none absolute -bottom-32 left-[-100px] h-72 w-72 rounded-full bg-indigo-200/50 blur-3xl dark:bg-indigo-900/20" />

      <div className="relative w-full max-w-md rounded-3xl border border-blue-100 bg-white/95 p-8 shadow-soft backdrop-blur dark:border-slate-700 dark:bg-slate-900/90">
        <div className="mb-6 flex items-center gap-3">
          <div className="rounded-xl bg-blue-600 p-2 text-white">
            <SparklesIcon className="h-5 w-5" />
          </div>
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.2em] text-blue-600">UNIGRAPH</p>
            <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-100">Welcome Back</h1>
          </div>
        </div>
        <p className="mb-5 text-sm text-slate-500 dark:text-slate-400">
          Sign in with your username and password. A JWT session is created automatically.
        </p>

        <form
          className="space-y-4"
          onSubmit={async (event) => {
            event.preventDefault();
            await onSubmit(username.trim(), password, sessionId.trim());
          }}
        >
          <label className="block text-sm font-medium text-slate-700 dark:text-slate-200">
            Username
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              className="mt-1 w-full rounded-xl border border-slate-200 px-4 py-2.5 text-slate-900 outline-none transition focus:border-blue-400 focus:ring-2 focus:ring-blue-200 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
              placeholder="admin"
              required
            />
          </label>

          <label className="block text-sm font-medium text-slate-700 dark:text-slate-200">
            Password
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="mt-1 w-full rounded-xl border border-slate-200 px-4 py-2.5 text-slate-900 outline-none transition focus:border-blue-400 focus:ring-2 focus:ring-blue-200 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
              placeholder="••••••••"
              required
            />
          </label>

          <label className="block text-sm font-medium text-slate-700 dark:text-slate-200">
            Session ID (optional)
            <input
              value={sessionId}
              onChange={(event) => setSessionId(event.target.value)}
              className="mt-1 w-full rounded-xl border border-slate-200 px-4 py-2.5 text-slate-900 outline-none transition focus:border-blue-400 focus:ring-2 focus:ring-blue-200 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
              placeholder="defaults to your user id"
            />
          </label>

          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 px-4 py-2.5 text-sm font-semibold text-white shadow-lg shadow-blue-500/30 transition hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {loading ? "Signing in..." : "Sign In"}
          </button>

          {error ? <p className="text-sm text-rose-600 dark:text-rose-400">{error}</p> : null}
        </form>
      </div>
    </div>
  );
}
