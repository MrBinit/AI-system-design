import { useEffect, useMemo, useRef, useState } from "react";

import { ChatInput } from "./components/ChatInput";
import { CheckCircleIcon, CloseIcon, GlobeIcon, LinkIcon, MenuIcon, SparklesIcon } from "./components/Icons";
import { LoginPanel } from "./components/LoginPanel";
import { MessageCard } from "./components/MessageCard";
import { Sidebar } from "./components/Sidebar";
import { clearChatHistory, fetchChatJobTrace, fetchConversations, loginWithPassword, streamChatResponse } from "./lib/api";
import type {
  AuthSession,
  ChatExecutionMode,
  ChatMessage,
  ConversationItem,
  ReactionType,
  StreamEvent,
  TraceEventItem,
} from "./types";

const AUTH_STORAGE_KEY = "ai.chat.frontend.auth";
const THEME_STORAGE_KEY = "ai.chat.frontend.theme";
const ACTIVE_JOB_STORAGE_KEY = "ai.chat.frontend.active_job_id";
const CHAT_MODE_STORAGE_KEY = "ai.chat.frontend.mode";
const CONVERSATION_META_STORAGE_KEY = "ai.chat.frontend.conversation_meta";

type ConversationDateFilter = "all" | "7d" | "30d";

interface ConversationMeta {
  pinned?: boolean;
  starred?: boolean;
  customTitle?: string;
}

interface PromptCommandResult {
  prompt: string;
  mode: ChatExecutionMode | null;
}

function makeId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function getStoredAuth(): AuthSession | null {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as AuthSession;
  } catch {
    return null;
  }
}

function getStoredTheme(): boolean {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) === "dark";
  } catch {
    return false;
  }
}

function getStoredChatMode(): ChatExecutionMode {
  try {
    const candidate = String(localStorage.getItem(CHAT_MODE_STORAGE_KEY) ?? "")
      .trim()
      .toLowerCase();
    if (candidate === "fast" || candidate === "deep" || candidate === "auto") {
      return candidate;
    }
  } catch {
    // ignore localStorage access failures and use default
  }
  return "auto";
}

function getStoredConversationMeta(): Record<string, ConversationMeta> {
  try {
    const raw = localStorage.getItem(CONVERSATION_META_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw) as Record<string, ConversationMeta>;
    if (!parsed || typeof parsed !== "object") {
      return {};
    }
    return parsed;
  } catch {
    return {};
  }
}

function parsePromptCommands(rawInput: string, defaultMode: ChatExecutionMode): PromptCommandResult {
  const text = rawInput.trim();
  if (!text.startsWith("/")) {
    return { prompt: text, mode: null };
  }

  const [rawCommand, ...rest] = text.split(/\s+/);
  const command = rawCommand.toLowerCase();
  const remaining = rest.join(" ").trim();
  let modeOverride: ChatExecutionMode | null = null;
  let prompt = remaining;

  if (command === "/fast") {
    modeOverride = "fast";
  } else if (command === "/deep") {
    modeOverride = "deep";
  } else if (command === "/auto") {
    modeOverride = "auto";
  } else if (command === "/cite") {
    prompt = remaining ? `${remaining}\n\nPlease include clear source citations.` : "";
  } else if (command === "/summarize") {
    prompt = remaining ? `Summarize clearly and concisely:\n\n${remaining}` : "";
  } else if (command === "/web") {
    prompt = remaining
      ? `${remaining}\n\nPrioritize web retrieval and provide source links for all key claims.`
      : "";
    modeOverride = defaultMode === "fast" ? "fast" : "deep";
  }

  return { prompt: prompt.trim(), mode: modeOverride };
}

function mergeStreamText(current: string, incoming: string): string {
  const next = incoming;
  if (!next) {
    return current;
  }
  if (!current) {
    return next;
  }
  if (next.startsWith(current)) {
    return next;
  }
  if (current.endsWith(next)) {
    return current;
  }
  return `${current}${next}`;
}

function streamEventText(event: StreamEvent): string {
  return String(event.text ?? event.delta ?? event.token ?? event.content ?? event.message ?? "");
}

function pendingPhaseText(status: string, elapsedSeconds: number): string {
  const normalized = status.trim().toLowerCase();
  if (normalized.includes("queued")) {
    return "Preparing request";
  }
  if (normalized.includes("search")) {
    return "Searching web and knowledge sources";
  }
  if (normalized.includes("retriev")) {
    return "Reviewing retrieved documents";
  }
  if (normalized.includes("rank") || normalized.includes("rerank")) {
    return "Ranking relevant evidence";
  }
  if (normalized.includes("draft") || normalized.includes("generat")) {
    return "Drafting response";
  }
  if (normalized.includes("check") || normalized.includes("validat")) {
    return "Cross-checking response";
  }

  if (elapsedSeconds < 4) {
    return "Searching web and knowledge sources";
  }
  if (elapsedSeconds < 8) {
    return "Reviewing relevant context";
  }
  if (elapsedSeconds < 12) {
    return "Drafting response";
  }
  return "Finalizing answer";
}

function mergeUniqueStrings(current: string[], incoming: string[], limit = 10): string[] {
  const deduped = new Set(current);
  for (const item of incoming) {
    const clean = item.trim();
    if (clean) {
      deduped.add(clean);
    }
  }
  return Array.from(deduped).slice(0, limit);
}

function displayWebsite(value: string): string {
  try {
    const hostname = new URL(value).hostname.trim().toLowerCase();
    return hostname.replace(/^www\./, "");
  } catch {
    return value.trim().toLowerCase();
  }
}

function extractUrlsFromText(input: string): string[] {
  const matches = input.match(/https?:\/\/[^\s)"']+/gi) ?? [];
  return Array.from(new Set(matches.map((item) => item.trim())));
}

const TRACE_EVENT_LABELS: Record<string, string> = {
  request_received: "Request received",
  query_plan_created: "Query plan created",
  query_planner_started: "Query planner started",
  query_planner_completed: "Query planner completed",
  query_planner_skipped: "Query planner skipped",
  search_started: "Search started",
  search_results: "Search results collected",
  pages_read: "Pages read",
  facts_extracted: "Facts extracted",
  gaps_identified: "Gaps identified",
  retrieval_verification: "Retrieval verification",
  source_ranking_completed: "Source ranking completed",
  retrieval_vector_started: "Vector retrieval started",
  retrieval_vector_completed: "Vector retrieval completed",
  web_retrieval_skipped: "Web retrieval skipped",
  web_fallback_started: "Web fallback started",
  web_fallback_completed: "Web fallback completed",
  retrieval_reranked: "Retrieval reranked",
  evidence_selected: "Evidence selected",
  citation_grounding_ready: "Citation grounding ready",
  answer_planning_started: "Answer planning started",
  answer_plan_created: "Answer plan created",
  answer_planning_completed: "Answer planning completed",
  model_round_started: "Model round started",
  model_round_completed: "Model round completed",
  answer_verification_completed: "Answer verification completed",
  answer_synthesis_completed: "Answer synthesis completed",
  answer_finalized: "Answer finalized",
  job_processing_started: "Job processing started",
  job_completed: "Job completed",
  job_failed: "Job failed",
};

function traceLabel(type: string): string {
  const normalized = type.trim().toLowerCase();
  return TRACE_EVENT_LABELS[normalized] ?? normalized.replace(/_/g, " ");
}

function traceTimeLabel(timestamp?: string): string {
  if (!timestamp) {
    return "";
  }
  const parsed = new Date(timestamp);
  if (!Number.isFinite(parsed.getTime())) {
    return "";
  }
  return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function compactUrlLabel(url: string): string {
  return url.replace(/^https?:\/\//i, "").replace(/\/$/, "");
}

function formatDurationLabel(totalSeconds: number): string {
  const safeSeconds = Math.max(1, Math.round(totalSeconds));
  if (safeSeconds < 60) {
    return `${safeSeconds}s`;
  }
  const minutes = Math.floor(safeSeconds / 60);
  const remainingSeconds = safeSeconds % 60;
  if (minutes < 60) {
    return remainingSeconds ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function mergeTraceEvents(current: TraceEventItem[], incoming: TraceEventItem[]): TraceEventItem[] {
  const seen = new Set(current.map((event) => `${event.type}|${event.timestamp ?? ""}`));
  const merged = [...current];
  for (const event of incoming) {
    const key = `${event.type}|${event.timestamp ?? ""}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(event);
  }
  return merged.slice(-120);
}

function extractTraceWebsites(payload?: Record<string, unknown>): string[] {
  if (!payload) {
    return [];
  }
  const urls = new Set<string>();
  const collect = (value: unknown) => {
    if (typeof value === "string") {
      const matches = value.match(/https?:\/\/[^\s)"']+/gi) ?? [];
      for (const match of matches) {
        urls.add(displayWebsite(match));
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        collect(item);
      }
      return;
    }
    if (value && typeof value === "object") {
      for (const nested of Object.values(value as Record<string, unknown>)) {
        collect(nested);
      }
    }
  };
  const sourceKeys = ["urls", "source_urls", "sources", "websites"];
  for (const key of sourceKeys) {
    collect(payload[key]);
  }
  return Array.from(urls);
}

function extractTraceUrls(payload?: Record<string, unknown>): string[] {
  if (!payload) {
    return [];
  }
  const urls = new Set<string>();
  const collect = (value: unknown) => {
    if (typeof value === "string") {
      const matches = value.match(/https?:\/\/[^\s)"']+/gi) ?? [];
      for (const match of matches) {
        urls.add(match.trim());
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        collect(item);
      }
      return;
    }
    if (value && typeof value === "object") {
      for (const nested of Object.values(value as Record<string, unknown>)) {
        collect(nested);
      }
    }
  };

  const urlKeys = ["urls", "source_urls", "sources", "websites", "citations", "results", "items"];
  for (const key of urlKeys) {
    collect(payload[key]);
  }
  return Array.from(urls);
}

function isLowSignalReasoningStep(step: string): boolean {
  const normalized = step.trim().toLowerCase();
  return (
    normalized === "queued" ||
    normalized === "processing" ||
    normalized === "status" ||
    normalized === "request received" ||
    normalized === "job processing started"
  );
}

export default function App() {
  const [auth, setAuth] = useState<AuthSession | null>(() => getStoredAuth());
  const [darkMode, setDarkMode] = useState<boolean>(() => getStoredTheme());
  const [chatMode, setChatMode] = useState<ChatExecutionMode>(() => getStoredChatMode());
  const [statusText, setStatusText] = useState("Ready");
  const [statusError, setStatusError] = useState(false);
  const [canCancelQueued, setCanCancelQueued] = useState(false);
  const [isAwaitingFirstChunk, setIsAwaitingFirstChunk] = useState(false);
  const [reasoningSteps, setReasoningSteps] = useState<string[]>([]);
  const [searchedWebsites, setSearchedWebsites] = useState<string[]>([]);
  const [workLogEvents, setWorkLogEvents] = useState<TraceEventItem[]>([]);
  const [activeJobId, setActiveJobId] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversations, setConversations] = useState<ConversationItem[]>([]);
  const [conversationMetaById, setConversationMetaById] = useState<Record<string, ConversationMeta>>(
    () => getStoredConversationMeta()
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [conversationDateFilter, setConversationDateFilter] = useState<ConversationDateFilter>("all");
  const [activeConversationId, setActiveConversationId] = useState("");
  const [inputValue, setInputValue] = useState("");
  const [loginError, setLoginError] = useState("");
  const [isLoadingLogin, setIsLoadingLogin] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [deletingConversationId, setDeletingConversationId] = useState<string | null>(null);
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [autoScrollPaused, setAutoScrollPaused] = useState(false);
  const [activeActivityMessageId, setActiveActivityMessageId] = useState("");

  const chatContainerRef = useRef<HTMLDivElement | null>(null);
  const messageEndRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const streamAbortControllerRef = useRef<AbortController | null>(null);

  const scrollToBottom = (behavior: ScrollBehavior, force = false) => {
    if (!force && autoScrollPaused) {
      return;
    }
    if (!force && !shouldAutoScrollRef.current) {
      return;
    }
    if (force) {
      shouldAutoScrollRef.current = true;
    }
    messageEndRef.current?.scrollIntoView({ behavior, block: "end" });
    setShowJumpToLatest(false);
  };

  const handleChatScroll = () => {
    const container = chatContainerRef.current;
    if (!container) {
      return;
    }
    const threshold = 120;
    const distanceFromBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight;
    const isNearBottom = distanceFromBottom <= threshold;
    shouldAutoScrollRef.current = isNearBottom;
    setShowJumpToLatest(!isNearBottom);
  };

  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
    localStorage.setItem(THEME_STORAGE_KEY, darkMode ? "dark" : "light");
  }, [darkMode]);

  useEffect(() => {
    localStorage.setItem(CHAT_MODE_STORAGE_KEY, chatMode);
  }, [chatMode]);

  useEffect(() => {
    localStorage.setItem(CONVERSATION_META_STORAGE_KEY, JSON.stringify(conversationMetaById));
  }, [conversationMetaById]);

  useEffect(() => {
    scrollToBottom(isSending ? "auto" : "smooth");
  }, [autoScrollPaused, isSending, messages]);

  useEffect(() => {
    if (!isSending && !isAwaitingFirstChunk) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      scrollToBottom("auto");
    });
    return () => {
      window.cancelAnimationFrame(frame);
    };
  }, [isAwaitingFirstChunk, isSending, reasoningSteps, searchedWebsites, workLogEvents]);

  useEffect(() => {
    const session = auth;
    if (!session) {
      setConversations([]);
      return;
    }

    const sessionUserId = session.userId;
    const sessionToken = session.token;
    let cancelled = false;

    async function loadHistory() {
      try {
        const history = await fetchConversations(sessionUserId, sessionToken, 80);
        if (cancelled) return;
        setConversations(history);
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : "Failed to load conversations.";
        setStatusText(message);
        setStatusError(true);
      }
    }

    void loadHistory();

    return () => {
      cancelled = true;
    };
  }, [auth]);

  useEffect(() => {
    const session = auth;
    if (!session) {
      setWorkLogEvents([]);
      setActiveJobId("");
      return;
    }
    const sessionToken = session.token;
    const savedJobId = String(localStorage.getItem(ACTIVE_JOB_STORAGE_KEY) ?? "").trim();
    if (!savedJobId) {
      return;
    }

    let cancelled = false;
    async function replayTrace() {
      try {
        const replay = await fetchChatJobTrace(sessionToken, savedJobId);
        if (cancelled) return;
        setActiveJobId(replay.jobId);
        setWorkLogEvents(replay.traceEvents);
        setReasoningSteps(
          mergeUniqueStrings([], replay.traceEvents.map((event) => traceLabel(event.type)), 12)
        );
        setSearchedWebsites((prev) =>
          mergeUniqueStrings(prev, replay.websites.map(displayWebsite), 12)
        );
        const terminal = replay.traceEvents.some((event) => {
          const kind = event.type.toLowerCase();
          return kind === "job_completed" || kind === "job_failed";
        });
        if (terminal) {
          localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
          setActiveJobId("");
        }
        setStatusText(`Recovered work log for job ${savedJobId.slice(0, 8)}...`);
        setStatusError(false);
      } catch {
        if (cancelled) return;
        localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
        setActiveJobId("");
      }
    }

    void replayTrace();
    return () => {
      cancelled = true;
    };
  }, [auth]);

  useEffect(() => {
    if (!auth || !activeJobId || !isSending) {
      return;
    }

    let cancelled = false;
    const pollTrace = async () => {
      try {
        const replay = await fetchChatJobTrace(auth.token, activeJobId);
        if (cancelled) return;
        setWorkLogEvents((prev) => mergeTraceEvents(prev, replay.traceEvents));
        setSearchedWebsites((prev) =>
          mergeUniqueStrings(prev, replay.websites.map(displayWebsite), 12)
        );
        const terminal = replay.traceEvents.some((event) => {
          const kind = event.type.toLowerCase();
          return kind === "job_completed" || kind === "job_failed";
        });
        if (terminal) {
          localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
          setActiveJobId("");
        }
      } catch {
        // ignore polling failures; live stream may still continue
      }
    };

    void pollTrace();
    const intervalId = window.setInterval(() => {
      void pollTrace();
    }, 2500);

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [auth, activeJobId, isSending]);

  useEffect(() => {
    if (!activeActivityMessageId) {
      return;
    }
    const exists = messages.some((message) => message.id === activeActivityMessageId);
    if (!exists) {
      setActiveActivityMessageId("");
    }
  }, [activeActivityMessageId, messages]);

  const enrichedConversations = useMemo(() => {
    return conversations.map((item) => {
      const meta = conversationMetaById[item.conversationId] ?? {};
      const customTitle = String(meta.customTitle ?? "").trim();
      return {
        ...item,
        title: customTitle || item.title,
      };
    });
  }, [conversationMetaById, conversations]);

  const filteredConversations = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    const now = Date.now();
    const cutoffMs =
      conversationDateFilter === "7d"
        ? 7 * 24 * 60 * 60 * 1000
        : conversationDateFilter === "30d"
          ? 30 * 24 * 60 * 60 * 1000
          : null;
    return enrichedConversations
      .filter((item) => {
        if (query) {
          const matches =
            item.title.toLowerCase().includes(query) ||
            item.prompt.toLowerCase().includes(query) ||
            item.answer.toLowerCase().includes(query);
          if (!matches) {
            return false;
          }
        }
        if (cutoffMs === null) {
          return true;
        }
        const createdAtMs = new Date(item.createdAt).getTime();
        return Number.isFinite(createdAtMs) && now - createdAtMs <= cutoffMs;
      })
      .sort((a, b) => {
        const aMeta = conversationMetaById[a.conversationId] ?? {};
        const bMeta = conversationMetaById[b.conversationId] ?? {};
        if (Boolean(aMeta.pinned) !== Boolean(bMeta.pinned)) {
          return aMeta.pinned ? -1 : 1;
        }
        if (Boolean(aMeta.starred) !== Boolean(bMeta.starred)) {
          return aMeta.starred ? -1 : 1;
        }
        return new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime();
      });
  }, [conversationDateFilter, conversationMetaById, enrichedConversations, searchQuery]);

  const yourConversations = useMemo(() => filteredConversations.slice(0, 24), [filteredConversations]);

  const lastSevenDays = useMemo(() => {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    return filteredConversations.filter((item) => {
      const time = new Date(item.createdAt).getTime();
      return Number.isFinite(time) && time >= cutoff;
    });
  }, [filteredConversations]);

  const followUpChips = useMemo(() => {
    const hasAssistantAnswer = messages.some((message) => message.role === "assistant");
    if (!hasAssistantAnswer) {
      return [
        "Give me a quick overview",
        "What should I ask next?",
        "Summarize this topic in bullets",
      ];
    }
    return [
      "Summarize this in 5 bullets",
      "What are the risks and tradeoffs?",
      "Compare top options in a table",
      "Give me a step-by-step action plan",
    ];
  }, [messages]);
  const selectedActivityMessage = useMemo(() => {
    if (!activeActivityMessageId) {
      return null;
    }
    return messages.find((message) => message.id === activeActivityMessageId) ?? null;
  }, [activeActivityMessageId, messages]);
  const selectedActivitySources = useMemo(() => {
    if (!selectedActivityMessage) {
      return [];
    }
    const messageSources = selectedActivityMessage.sourceUrls ?? [];
    const inlineSources = extractUrlsFromText(selectedActivityMessage.content);
    return Array.from(new Set([...messageSources, ...inlineSources])).slice(0, 30);
  }, [selectedActivityMessage]);

  const updateMessage = (messageId: string, updater: (message: ChatMessage) => ChatMessage) => {
    setMessages((prev) => prev.map((item) => (item.id === messageId ? updater(item) : item)));
  };

  const refreshHistory = async () => {
    if (!auth) return;
    const history = await fetchConversations(auth.userId, auth.token, 80);
    setConversations(history);
  };

  const sendPrompt = async (
    prompt: string,
    includeUserMessage = true,
    options?: { modeOverride?: ChatExecutionMode; statusLabel?: string }
  ) => {
    if (!auth || isSending) {
      return;
    }

    const cleanPrompt = prompt.trim();
    if (!cleanPrompt) {
      return;
    }

    const requestedMode = options?.modeOverride ?? chatMode;

    setIsSending(true);
    setStatusError(false);
    setStatusText(options?.statusLabel ?? (includeUserMessage ? "Submitting prompt..." : "Regenerating response..."));
    setCanCancelQueued(false);
    setIsAwaitingFirstChunk(true);
    setReasoningSteps([]);
    setSearchedWebsites([]);
    setWorkLogEvents([]);
    setActiveActivityMessageId("");
    setActiveJobId("");
    localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
    const streamAbortController = new AbortController();
    streamAbortControllerRef.current = streamAbortController;

    const assistantId = makeId();

    if (includeUserMessage) {
      const userMessage: ChatMessage = {
        id: makeId(),
        role: "user",
        username: auth.username,
        content: cleanPrompt,
        createdAt: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, userMessage]);
    }

    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      username: "UNIGRAPH",
      content: "Thinking...",
      createdAt: new Date().toISOString(),
      sourcePrompt: cleanPrompt,
      executionMode: requestedMode,
      reaction: null,
    };

    setMessages((prev) => [...prev, assistantMessage]);
    shouldAutoScrollRef.current = true;
    scrollToBottom("smooth", true);

    let streamTargetText = "";
    let streamRenderedText = "";
    let doneReceived = false;
    let firstTokenReceived = false;
    let pumpResolved = false;
    const waitingFrames = ["", ".", "..", "..."];
    let waitingFrameIndex = 0;
    const pendingStartedAt = Date.now();
    const traceSourceUrls = new Set<string>();
    const traceTimestampMs: number[] = [];
    let messageReasoningSteps: string[] = [];
    let messageSearchedWebsites: string[] = [];
    const messageTraceEvents: TraceEventItem[] = [];
    let latestServerStatus = "queued";
    const thinkingInterval = window.setInterval(() => {
      if (firstTokenReceived) {
        return;
      }
      waitingFrameIndex = (waitingFrameIndex + 1) % waitingFrames.length;
      const elapsedSeconds = Math.max(1, Math.floor((Date.now() - pendingStartedAt) / 1000));
      const phase = pendingPhaseText(latestServerStatus, elapsedSeconds);
      updateMessage(assistantId, (item) => ({
        ...item,
        content: `${phase}${waitingFrames[waitingFrameIndex]}\nStill working (${elapsedSeconds}s)`,
      }));
    }, 350);
    let pumpIntervalId: number | null = null;
    let resolvePump: (() => void) | null = null;
    const pumpDone = new Promise<void>((resolve) => {
      resolvePump = resolve;
    });

    const stopThinking = () => {
      window.clearInterval(thinkingInterval);
    };
    const addReasoningSteps = (steps: string[] | undefined) => {
      if (!steps?.length) {
        return;
      }
      const filtered = steps.filter((step) => !isLowSignalReasoningStep(step));
      if (!filtered.length) {
        return;
      }
      messageReasoningSteps = mergeUniqueStrings(messageReasoningSteps, filtered, 12);
      setReasoningSteps((prev) => mergeUniqueStrings(prev, filtered, 8));
    };
    const addSearchedWebsites = (websites: string[] | undefined) => {
      if (!websites?.length) {
        return;
      }
      const normalized = websites.map(displayWebsite);
      messageSearchedWebsites = mergeUniqueStrings(messageSearchedWebsites, normalized, 15);
      setSearchedWebsites((prev) => mergeUniqueStrings(prev, normalized, 8));
    };
    const setCurrentJob = (jobId: string | undefined) => {
      const clean = String(jobId ?? "").trim();
      if (!clean) {
        return;
      }
      setActiveJobId(clean);
      localStorage.setItem(ACTIVE_JOB_STORAGE_KEY, clean);
    };
    const appendTraceEvent = (trace: TraceEventItem | undefined) => {
      if (!trace) {
        return;
      }
      messageTraceEvents.push(trace);
      for (const url of extractTraceUrls(trace.payload)) {
        traceSourceUrls.add(url);
      }
      if (trace.timestamp) {
        const traceTimeMs = new Date(trace.timestamp).getTime();
        if (Number.isFinite(traceTimeMs)) {
          traceTimestampMs.push(traceTimeMs);
        }
      }
      setWorkLogEvents((prev) => mergeTraceEvents(prev, [trace]));
      addReasoningSteps([traceLabel(trace.type)]);
      addSearchedWebsites(extractTraceWebsites(trace.payload));
      setStatusText(`Status: ${traceLabel(trace.type)}`);
      setStatusError(false);
    };
    const finalizeAssistantMeta = (content: string) => {
      const finalizedAt = Date.now();
      const startedAt = traceTimestampMs.length ? Math.min(pendingStartedAt, ...traceTimestampMs) : pendingStartedAt;
      const endedAt = traceTimestampMs.length ? Math.max(finalizedAt, ...traceTimestampMs) : finalizedAt;
      const workedForLabel = formatDurationLabel((endedAt - startedAt) / 1000);
      const sourceUrls = Array.from(
        new Set([...traceSourceUrls, ...extractUrlsFromText(content)])
      ).slice(0, 12);
      updateMessage(assistantId, (item) => ({
        ...item,
        workedForLabel,
        sourceUrls,
        reasoningSteps: messageReasoningSteps,
        searchedWebsites: messageSearchedWebsites,
        traceEvents: messageTraceEvents.slice(-50),
      }));
    };
    const maybeResolvePump = () => {
      if (pumpResolved || !doneReceived) {
        return;
      }
      if (streamRenderedText.length < streamTargetText.length) {
        return;
      }
      if (pumpIntervalId !== null) {
        window.clearInterval(pumpIntervalId);
      }
      updateMessage(assistantId, (item) => ({
        ...item,
        content: streamRenderedText || "(No response text returned.)",
      }));
      pumpResolved = true;
      resolvePump?.();
    };
    const startRenderPump = () => {
      if (pumpIntervalId !== null) {
        return;
      }
      pumpIntervalId = window.setInterval(() => {
        if (streamRenderedText.length < streamTargetText.length) {
          const nextLength = Math.min(streamTargetText.length, streamRenderedText.length + 4);
          streamRenderedText = streamTargetText.slice(0, nextLength);
          updateMessage(assistantId, (item) => ({ ...item, content: `${streamRenderedText}▍` }));
          return;
        }
        maybeResolvePump();
      }, 16);
    };

    try {
      await streamChatResponse(
        auth.token,
        {
          userId: auth.userId,
          sessionId: auth.sessionId || auth.userId,
          prompt: cleanPrompt,
          mode: requestedMode,
        },
        (event: StreamEvent) => {
          if (event.type === "queued") {
            latestServerStatus = event.status ?? "queued";
            setCurrentJob(event.job_id);
            addSearchedWebsites(event.websites);
            setCanCancelQueued(true);
            setStatusText("Status: Preparing request");
            setStatusError(false);
            return;
          }
          if (event.type === "status") {
            latestServerStatus = event.status ?? latestServerStatus;
            setCurrentJob(event.job_id);
            addSearchedWebsites(event.websites);
            const isQueuedStatus = String(latestServerStatus).toLowerCase().includes("queued");
            setCanCancelQueued(isQueuedStatus && !firstTokenReceived);
            const elapsedSeconds = Math.max(1, Math.floor((Date.now() - pendingStartedAt) / 1000));
            setStatusText(`Status: ${pendingPhaseText(latestServerStatus, elapsedSeconds)}`);
            setStatusError(false);
            return;
          }
          if (event.type === "trace") {
            setCurrentJob(event.job_id);
            appendTraceEvent(event.trace);
            addSearchedWebsites(event.websites);
            return;
          }
          if (event.type === "search") {
            addSearchedWebsites(event.websites);
            setStatusText("Status: Searching external sources");
            setStatusError(false);
            return;
          }
          if (event.type === "reasoning") {
            addReasoningSteps(event.steps);
            if (event.steps?.[0]) {
              setStatusText(`Status: ${event.steps[0]}`);
            }
            setStatusError(false);
            return;
          }
          if (event.type === "chunk") {
            const delta = streamEventText(event);
            if (!delta) {
              return;
            }
            if (!firstTokenReceived) {
              firstTokenReceived = true;
              stopThinking();
              startRenderPump();
              setCanCancelQueued(false);
              setIsAwaitingFirstChunk(false);
            }
            streamTargetText = mergeStreamText(streamTargetText, delta);
            setStatusText("Streaming response...");
            setStatusError(false);
            return;
          }
          if (event.type === "done") {
            doneReceived = true;
            stopThinking();
            maybeResolvePump();
            setCanCancelQueued(false);
            setIsAwaitingFirstChunk(false);
            setActiveJobId("");
            localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
            setStatusText("Complete");
            setStatusError(false);
            return;
          }
          if (event.type === "error") {
            throw new Error(event.detail ?? "Chat stream failed.");
          }
        },
        { signal: streamAbortController.signal }
      );

      stopThinking();
      doneReceived = true;
      if (!firstTokenReceived) {
        streamTargetText = streamTargetText || "";
        streamRenderedText = streamTargetText;
        maybeResolvePump();
      } else {
        maybeResolvePump();
        await pumpDone;
      }

      if (streamTargetText) {
        setStatusText("Complete");
        setStatusError(false);
      }
      finalizeAssistantMeta(streamTargetText || streamRenderedText);

      setInputValue("");
      await refreshHistory();
    } catch (error) {
      stopThinking();
      if (pumpIntervalId !== null) {
        window.clearInterval(pumpIntervalId);
      }
      const isAbortError =
        (error instanceof DOMException && error.name === "AbortError") ||
        (error instanceof Error && error.name === "AbortError");
      if (isAbortError) {
        updateMessage(assistantId, (item) => ({
          ...item,
          content: "Request canceled.",
          workedForLabel: formatDurationLabel((Date.now() - pendingStartedAt) / 1000),
          sourceUrls: Array.from(traceSourceUrls),
          reasoningSteps: messageReasoningSteps,
          searchedWebsites: messageSearchedWebsites,
          traceEvents: messageTraceEvents.slice(-50),
        }));
        setActiveJobId("");
        localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
        setStatusText("Request canceled.");
        setStatusError(false);
      } else {
        const message = error instanceof Error ? error.message : "Chat request failed.";
        const errorText = `Error: ${message}`;
        updateMessage(assistantId, (item) => ({
          ...item,
          content: errorText,
          workedForLabel: formatDurationLabel((Date.now() - pendingStartedAt) / 1000),
          sourceUrls: Array.from(new Set([...traceSourceUrls, ...extractUrlsFromText(errorText)])).slice(0, 12),
          reasoningSteps: messageReasoningSteps,
          searchedWebsites: messageSearchedWebsites,
          traceEvents: messageTraceEvents.slice(-50),
        }));
        setActiveJobId("");
        localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
        setStatusText(message);
        setStatusError(true);
      }
    } finally {
      stopThinking();
      if (pumpIntervalId !== null) {
        window.clearInterval(pumpIntervalId);
      }
      setCanCancelQueued(false);
      setIsAwaitingFirstChunk(false);
      streamAbortControllerRef.current = null;
      setIsSending(false);
    }
  };

  const handleCancelQueuedRequest = () => {
    if (!canCancelQueued) {
      return;
    }
    setStatusText("Canceling request...");
    setStatusError(false);
    setCanCancelQueued(false);
    streamAbortControllerRef.current?.abort();
  };

  const handleLogin = async (username: string, password: string, sessionId: string) => {
    setIsLoadingLogin(true);
    setLoginError("");

    try {
      const payload = await loginWithPassword(username, password);
      const session: AuthSession = {
        token: payload.access_token,
        userId: payload.user_id,
        username,
        roles: payload.roles,
        sessionId: sessionId || payload.user_id,
      };
      setAuth(session);
      localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
      setStatusText("Signed in");
      setStatusError(false);
    } catch (error) {
      setLoginError(error instanceof Error ? error.message : "Sign-in failed.");
    } finally {
      setIsLoadingLogin(false);
    }
  };

  const handleConversationSelect = (conversation: ConversationItem) => {
    setActiveConversationId(conversation.conversationId);
    setReasoningSteps([]);
    setSearchedWebsites([]);
    setWorkLogEvents([]);
    setActiveActivityMessageId("");
    setActiveJobId("");
    localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
    setMessages([
      {
        id: makeId(),
        role: "user",
        username: auth?.username || "You",
        content: conversation.prompt,
        createdAt: conversation.createdAt,
      },
      {
        id: makeId(),
        role: "assistant",
        username: "UNIGRAPH",
        content: conversation.answer,
        createdAt: conversation.createdAt,
        sourcePrompt: conversation.prompt,
        sourceUrls: extractUrlsFromText(conversation.answer).slice(0, 12),
        reaction: null,
      },
    ]);
    setStatusText(`Loaded conversation: ${conversation.title}`);
    setStatusError(false);
  };

  const handleRegenerate = async (sourcePrompt: string) => {
    await sendPrompt(sourcePrompt, false);
  };

  const handleRegenerateInDeep = async (sourcePrompt: string) => {
    await sendPrompt(sourcePrompt, false, { modeOverride: "deep", statusLabel: "Regenerating in deep mode..." });
  };

  const handleRetryWebOnly = async (sourcePrompt: string) => {
    const webFirstPrompt = `${sourcePrompt}\n\nPrioritize web retrieval and include direct source URLs for key claims.`;
    await sendPrompt(webFirstPrompt, false, { modeOverride: "deep", statusLabel: "Retrying with web-first strategy..." });
  };

  const handleReaction = (messageId: string, reaction: ReactionType) => {
    updateMessage(messageId, (item) => ({ ...item, reaction }));
  };

  const handleOpenActivity = (messageId: string) => {
    setActiveActivityMessageId(messageId);
  };

  const handleSubmitFromComposer = async (rawInput: string) => {
    const parsed = parsePromptCommands(rawInput, chatMode);
    if (!parsed.prompt) {
      setStatusText("Write a prompt after the slash command.");
      setStatusError(true);
      return;
    }
    if (parsed.mode && parsed.mode !== chatMode) {
      setChatMode(parsed.mode);
    }
    await sendPrompt(parsed.prompt, true, {
      modeOverride: parsed.mode ?? chatMode,
      statusLabel: parsed.mode ? `Submitting prompt in ${parsed.mode} mode...` : "Submitting prompt...",
    });
  };

  const handleStopGeneration = () => {
    if (!isSending) {
      return;
    }
    setStatusText("Stopping generation...");
    setStatusError(false);
    setCanCancelQueued(false);
    streamAbortControllerRef.current?.abort();
  };

  const handleDeleteConversation = async (conversation: ConversationItem) => {
    if (!auth || deletingConversationId) {
      return;
    }

    setDeletingConversationId(conversation.conversationId);
    setStatusError(false);
    setStatusText("Deleting conversation...");

    try {
      await clearChatHistory(auth.userId, auth.token, conversation.conversationId);
      setConversations((prev) =>
        prev.filter((item) => item.conversationId !== conversation.conversationId)
      );
      setConversationMetaById((prev) => {
        const next = { ...prev };
        delete next[conversation.conversationId];
        return next;
      });
      if (activeConversationId === conversation.conversationId) {
        setMessages([]);
        setActiveConversationId("");
        setInputValue("");
      }
      setStatusText("Conversation deleted.");
      setStatusError(false);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to delete conversation.";
      setStatusText(message);
      setStatusError(true);
    } finally {
      setDeletingConversationId(null);
    }
  };

  const isPinnedConversation = (conversationId: string) =>
    Boolean(conversationMetaById[conversationId]?.pinned);
  const isStarredConversation = (conversationId: string) =>
    Boolean(conversationMetaById[conversationId]?.starred);

  const handleTogglePinnedConversation = (conversationId: string) => {
    setConversationMetaById((prev) => {
      const current = prev[conversationId] ?? {};
      return {
        ...prev,
        [conversationId]: {
          ...current,
          pinned: !current.pinned,
        },
      };
    });
  };

  const handleToggleStarredConversation = (conversationId: string) => {
    setConversationMetaById((prev) => {
      const current = prev[conversationId] ?? {};
      return {
        ...prev,
        [conversationId]: {
          ...current,
          starred: !current.starred,
        },
      };
    });
  };

  const handleRenameConversation = (conversation: ConversationItem) => {
    const nextTitle = window.prompt("Rename conversation", conversation.title);
    if (nextTitle === null) {
      return;
    }
    const trimmed = nextTitle.trim();
    setConversationMetaById((prev) => {
      const current = prev[conversation.conversationId] ?? {};
      return {
        ...prev,
        [conversation.conversationId]: {
          ...current,
          customTitle: trimmed || undefined,
        },
      };
    });
  };

  const handleLogout = () => {
    streamAbortControllerRef.current?.abort();
    streamAbortControllerRef.current = null;
    localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
    localStorage.removeItem(AUTH_STORAGE_KEY);
    setAuth(null);
    setMessages([]);
    setConversations([]);
    setActiveConversationId("");
    setInputValue("");
    setReasoningSteps([]);
    setSearchedWebsites([]);
    setWorkLogEvents([]);
    setActiveActivityMessageId("");
    setActiveJobId("");
    setCanCancelQueued(false);
    setIsAwaitingFirstChunk(false);
    setStatusText("Signed out");
    setStatusError(false);
  };

  if (!auth) {
    return <LoginPanel loading={isLoadingLogin} error={loginError} onSubmit={handleLogin} />;
  }

  return (
    <div className="min-h-screen bg-app-gradient text-slate-800 dark:bg-app-gradient-dark dark:text-slate-100">
      <Sidebar
        auth={auth}
        conversations={yourConversations}
        recentConversations={lastSevenDays}
        activeConversationId={activeConversationId}
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        dateFilter={conversationDateFilter}
        onDateFilterChange={setConversationDateFilter}
        onNewChat={() => {
          setMessages([]);
          setActiveConversationId("");
          setReasoningSteps([]);
          setSearchedWebsites([]);
          setWorkLogEvents([]);
          setActiveActivityMessageId("");
          setActiveJobId("");
          localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
          setStatusText("New chat started");
          setStatusError(false);
          setIsSidebarOpen(false);
        }}
        onDeleteConversation={async (conversation) => {
          await handleDeleteConversation(conversation);
          setIsSidebarOpen(false);
        }}
        deletingConversationId={deletingConversationId}
        isPinned={isPinnedConversation}
        isStarred={isStarredConversation}
        onTogglePin={handleTogglePinnedConversation}
        onToggleStar={handleToggleStarredConversation}
        onRenameConversation={handleRenameConversation}
        onSelectConversation={handleConversationSelect}
        onToggleTheme={() => setDarkMode((prev) => !prev)}
        darkMode={darkMode}
        mobileOpen={isSidebarOpen}
        onCloseMobile={() => setIsSidebarOpen(false)}
      />

      <div className="md:ml-[260px]">
        <header className="sticky top-0 z-20 border-b border-blue-100 bg-white/85 px-5 py-4 backdrop-blur dark:border-slate-800 dark:bg-slate-950/75">
          <div className="mx-auto flex w-full max-w-4xl items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="rounded-lg p-1.5 text-slate-600 hover:bg-blue-50 hover:text-blue-700 dark:text-slate-300 dark:hover:bg-slate-800 md:hidden"
                onClick={() => setIsSidebarOpen(true)}
                aria-label="Open sidebar"
              >
                <MenuIcon className="h-5 w-5" />
              </button>
              <SparklesIcon className="h-5 w-5 text-blue-600" />
              <h1 className="text-lg font-semibold">UNIGRAPH</h1>
            </div>
            <div className="flex items-start gap-3 text-sm text-slate-500 dark:text-slate-400">
              <div className="max-w-[320px]">
                <span className={statusError ? "rounded-md bg-rose-50 px-2 py-1 text-rose-600 dark:bg-rose-950/40 dark:text-rose-300" : ""}>
                  {statusText}
                </span>
                {isAwaitingFirstChunk ? (
                  <div className="mt-1 flex items-center gap-1 text-[11px] text-slate-500 dark:text-slate-400">
                    {[0, 1, 2].map((index) => (
                      <span
                        key={index}
                        className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500/80 dark:bg-blue-300/80"
                        style={{ animationDelay: `${index * 160}ms` }}
                      />
                    ))}
                    <span className="ml-1">Working...</span>
                  </div>
                ) : null}
                {reasoningSteps.length ? (
                  <details className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                    <summary className="cursor-pointer select-none">Reasoning progress</summary>
                    <ul className="mt-1 space-y-0.5 rounded bg-slate-100 px-2 py-1 text-[11px] text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                      {reasoningSteps.map((step) => (
                        <li key={step}>- {step}</li>
                      ))}
                    </ul>
                  </details>
                ) : null}
                {searchedWebsites.length ? (
                  <details className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                    <summary className="cursor-pointer select-none">Websites searched</summary>
                    <div className="mt-1 flex flex-wrap gap-1 rounded bg-slate-100 px-2 py-1 dark:bg-slate-800">
                      {searchedWebsites.map((site) => (
                        <code key={site} className="rounded bg-white px-1.5 py-0.5 text-[11px] text-slate-700 dark:bg-slate-900 dark:text-slate-200">
                          {site}
                        </code>
                      ))}
                    </div>
                  </details>
                ) : null}
              </div>
              {isSending ? (
                <button
                  type="button"
                  onClick={canCancelQueued ? handleCancelQueuedRequest : handleStopGeneration}
                  className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-700 hover:bg-amber-100 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200 dark:hover:bg-amber-900/40"
                >
                  {canCancelQueued ? "Cancel queued" : "Stop generation"}
                </button>
              ) : null}
              <button
                type="button"
                onClick={handleLogout}
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
              >
                Logout
              </button>
            </div>
          </div>
        </header>

        <main
          ref={chatContainerRef}
          onScroll={handleChatScroll}
          className="h-[calc(100vh-154px)] overflow-y-auto"
        >
          <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col gap-4 px-4 pb-44 pt-6">

            {messages.length ? (
              messages.map((message) => (
                <MessageCard
                  key={message.id}
                  message={message}
                  onRegenerate={handleRegenerate}
                  onRegenerateInDeep={handleRegenerateInDeep}
                  onRetryWebOnly={handleRetryWebOnly}
                  onOpenActivity={handleOpenActivity}
                  onReaction={handleReaction}
                />
              ))
            ) : (
              <div className="mt-20 rounded-3xl border border-blue-200 bg-white/85 p-10 text-center shadow-soft dark:border-slate-700 dark:bg-slate-900/60">
                <h2 className="mb-2 text-2xl font-semibold">Start a new conversation</h2>
                <p className="text-sm text-slate-500 dark:text-slate-400">
                  Ask anything about your data and get a streamed AI response in a clean ChatGPT-style workspace.
                </p>
              </div>
            )}
            <div ref={messageEndRef} />
          </div>
        </main>

        {selectedActivityMessage ? (
          <>
            <button
              type="button"
              className="fixed inset-0 z-30 bg-slate-950/25 lg:hidden"
              aria-label="Close activity panel"
              onClick={() => setActiveActivityMessageId("")}
            />
            <aside className="fixed right-0 top-0 z-40 h-full w-full max-w-[392px] border-l border-blue-100 bg-white shadow-2xl dark:border-slate-800 dark:bg-slate-950 lg:right-3 lg:top-3 lg:h-[calc(100%-1.5rem)] lg:rounded-2xl lg:border">
              <div className="flex h-full flex-col">
                <div className="flex items-center justify-between border-b border-blue-100 bg-gradient-to-b from-blue-50/70 to-white px-4 py-3.5 dark:border-slate-800 dark:from-slate-900/90 dark:to-slate-950">
                  <div>
                    <p className="text-[15px] font-semibold text-slate-800 dark:text-slate-100">
                      Activity {selectedActivityMessage.workedForLabel ? `· ${selectedActivityMessage.workedForLabel}` : ""}
                    </p>
                    <p className="text-xs text-slate-500 dark:text-slate-400">
                      {new Date(selectedActivityMessage.createdAt).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => setActiveActivityMessageId("")}
                    className="rounded-lg border border-slate-200 p-1.5 text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                    aria-label="Close activity panel"
                  >
                    <CloseIcon className="h-4 w-4" />
                  </button>
                </div>

                <div className="flex-1 space-y-5 overflow-y-auto px-4 py-4">
                  <section className="rounded-2xl border border-blue-100 bg-blue-50/35 p-3 dark:border-slate-800 dark:bg-slate-900/40">
                    <div className="mb-2.5 flex items-center gap-2 text-[15px] font-semibold text-slate-800 dark:text-slate-100">
                      <CheckCircleIcon className="h-4 w-4 text-emerald-600 dark:text-emerald-400" />
                      Thinking
                    </div>
                    {selectedActivityMessage.traceEvents?.length ? (
                      <ul className="space-y-1.5">
                        {selectedActivityMessage.traceEvents.slice(-14).map((event, index) => {
                          const websites = extractTraceWebsites(event.payload).slice(0, 4);
                          return (
                            <li
                              key={`${event.type}-${event.timestamp ?? index}`}
                              className="rounded-xl border border-blue-100 bg-white p-2.5 dark:border-slate-700 dark:bg-slate-900"
                            >
                              <div className="flex items-start justify-between gap-2">
                                <p className="text-[13px] font-medium capitalize text-slate-700 dark:text-slate-200">
                                  {traceLabel(event.type)}
                                </p>
                                <span className="text-[10px] text-slate-500 dark:text-slate-400">
                                  {traceTimeLabel(event.timestamp)}
                                </span>
                              </div>
                              {websites.length ? (
                                <div className="mt-1.5 flex flex-wrap gap-1">
                                  {websites.map((site) => (
                                    <span
                                      key={`${event.type}-${site}`}
                                      className="rounded bg-white px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                                    >
                                      {site}
                                    </span>
                                  ))}
                                </div>
                              ) : null}
                            </li>
                          );
                        })}
                      </ul>
                    ) : selectedActivityMessage.reasoningSteps?.length ? (
                      <ul className="space-y-1.5">
                        {selectedActivityMessage.reasoningSteps.slice(-10).map((step) => (
                          <li key={step} className="rounded-xl border border-blue-100 bg-white px-2.5 py-2 text-[13px] text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200">
                            {step}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-xs text-slate-500 dark:text-slate-400">No thought details captured for this response.</p>
                    )}
                  </section>

                  <section className="rounded-2xl border border-blue-100 bg-blue-50/35 p-3 dark:border-slate-800 dark:bg-slate-900/40">
                    <div className="mb-2.5 flex items-center gap-2 text-[15px] font-semibold text-slate-800 dark:text-slate-100">
                      <GlobeIcon className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                      Websites searched
                    </div>
                    {selectedActivityMessage.searchedWebsites?.length ? (
                      <div className="flex flex-wrap gap-1.5">
                        {selectedActivityMessage.searchedWebsites.map((site) => (
                          <span
                            key={site}
                            className="rounded-full border border-blue-200 bg-white px-2 py-0.5 text-[11px] text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
                          >
                            {site}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <p className="text-xs text-slate-500 dark:text-slate-400">No website list available.</p>
                    )}
                  </section>

                  <section className="rounded-2xl border border-blue-100 bg-blue-50/35 p-3 dark:border-slate-800 dark:bg-slate-900/40">
                    <div className="mb-2.5 flex items-center gap-2 text-[15px] font-semibold text-slate-800 dark:text-slate-100">
                      <LinkIcon className="h-4 w-4 text-indigo-600 dark:text-indigo-400" />
                      Sources · {selectedActivitySources.length}
                    </div>
                    {selectedActivitySources.length ? (
                      <div className="space-y-1.5">
                        {selectedActivitySources.slice(0, 20).map((url) => (
                          <a
                            key={url}
                            href={url}
                            target="_blank"
                            rel="noreferrer"
                            className="block rounded-xl border border-blue-100 bg-white px-2.5 py-2 text-xs hover:bg-blue-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
                          >
                            <span className="block text-[12px] font-medium text-slate-700 dark:text-slate-200">
                              {displayWebsite(url)}
                            </span>
                            <span className="mt-0.5 block truncate text-[11px] text-blue-700 dark:text-blue-300">
                              {compactUrlLabel(url)}
                            </span>
                          </a>
                        ))}
                      </div>
                    ) : (
                      <p className="text-xs text-slate-500 dark:text-slate-400">No citations were captured.</p>
                    )}
                  </section>
                </div>
              </div>
            </aside>
          </>
        ) : null}

        {showJumpToLatest && messages.length ? (
          <button
            type="button"
            onClick={() => {
              shouldAutoScrollRef.current = true;
              scrollToBottom("smooth", true);
            }}
            className="fixed bottom-28 right-4 z-30 rounded-full border border-blue-200 bg-white/95 px-3 py-1.5 text-xs font-medium text-blue-700 shadow-soft hover:bg-blue-50 dark:border-slate-700 dark:bg-slate-900/95 dark:text-blue-300 dark:hover:bg-slate-800 md:right-8"
          >
            Jump to latest
          </button>
        ) : null}

        <div className="fixed bottom-0 left-0 right-0 border-t border-blue-100 bg-white/75 backdrop-blur-md dark:border-slate-800 dark:bg-slate-950/80 md:left-[260px]">
          <ChatInput
            value={inputValue}
            disabled={isSending}
            mode={chatMode}
            suggestionChips={followUpChips}
            autoScrollPaused={autoScrollPaused}
            onChange={setInputValue}
            onModeChange={setChatMode}
            onSuggestionClick={async (chip) => {
              await handleSubmitFromComposer(chip);
            }}
            onToggleAutoScroll={() => {
              setAutoScrollPaused((prev) => {
                const next = !prev;
                if (next) {
                  shouldAutoScrollRef.current = false;
                  setShowJumpToLatest(true);
                } else {
                  shouldAutoScrollRef.current = true;
                  scrollToBottom("smooth", true);
                }
                return next;
              });
            }}
            onSubmit={async () => {
              await handleSubmitFromComposer(inputValue);
            }}
          />
        </div>
      </div>
    </div>
  );
}
