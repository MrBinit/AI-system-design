import { useEffect, useMemo, useRef, useState } from "react";

import { ChatInput } from "./components/ChatInput";
import { LoginPanel } from "./components/LoginPanel";
import { MenuIcon, SparklesIcon } from "./components/Icons";
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

type PipelineFinalStage = "pending" | "streaming" | "finalized" | "failed";

interface PipelineTimelineItem {
  key: string;
  label: string;
  detail: string;
  timestamp?: string;
  websites: string[];
}

interface PipelineView {
  plannerType: string;
  plannerLlm: string;
  plannerSkippedReason: string;
  plannedQueries: string[];
  subquestions: string[];
  retrievalQueries: string[];
  websites: string[];
  retrievalVerificationPassed: boolean | null;
  answerVerificationPassed: boolean | null;
  retrievalIssues: string[];
  answerIssues: string[];
  sourceUrls: string[];
  finalStage: PipelineFinalStage;
  finalizedAt: string;
  timeline: PipelineTimelineItem[];
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
    return timestamp;
  }
  return parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
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

function payloadString(payload: Record<string, unknown> | undefined, keys: string[]): string {
  if (!payload) {
    return "";
  }
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string") {
      const clean = value.trim();
      if (clean) {
        return clean;
      }
    }
  }
  return "";
}

function payloadBool(payload: Record<string, unknown> | undefined, keys: string[]): boolean | null {
  if (!payload) {
    return null;
  }
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "boolean") {
      return value;
    }
    if (typeof value === "string") {
      const normalized = value.trim().toLowerCase();
      if (["true", "pass", "passed", "ok", "success"].includes(normalized)) return true;
      if (["false", "fail", "failed", "error"].includes(normalized)) return false;
    }
  }
  return null;
}

function payloadStringList(payload: Record<string, unknown> | undefined, keys: string[]): string[] {
  if (!payload) {
    return [];
  }
  const values: string[] = [];
  const collect = (input: unknown) => {
    if (typeof input === "string") {
      const clean = input.trim();
      if (clean) values.push(clean);
      return;
    }
    if (Array.isArray(input)) {
      for (const item of input) collect(item);
      return;
    }
    if (input && typeof input === "object") {
      const record = input as Record<string, unknown>;
      const candidateFields = [
        "text",
        "query",
        "question",
        "issue",
        "reason",
        "url",
        "source_url",
        "title",
        "message",
      ];
      for (const field of candidateFields) {
        const value = record[field];
        if (typeof value === "string") {
          const clean = value.trim();
          if (clean) values.push(clean);
        }
      }
    }
  };
  for (const key of keys) {
    collect(payload[key]);
  }
  return Array.from(new Set(values));
}

function payloadNumber(payload: Record<string, unknown> | undefined, keys: string[]): number | null {
  if (!payload) {
    return null;
  }
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string") {
      const parsed = Number(value);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }
  return null;
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

function traceEventDetail(type: string, payload?: Record<string, unknown>): string {
  const normalized = type.trim().toLowerCase();
  const query = payloadString(payload, ["query", "search_query"]);
  const queries = payloadStringList(payload, ["queries", "search_queries", "planned_queries"]).slice(0, 2);
  const reason = payloadString(payload, ["reason", "skip_reason", "message"]);

  if (normalized === "query_planner_started" || normalized === "query_plan_created") {
    const planner = payloadString(payload, ["planner_type", "planner", "type", "strategy"]);
    const llm = payloadString(payload, ["llm_used", "planner_llm", "model", "llm"]);
    return [planner ? `planner: ${planner}` : "", llm ? `llm: ${llm}` : ""].filter(Boolean).join(" | ");
  }
  if (normalized === "query_planner_skipped" || normalized === "web_retrieval_skipped") {
    return reason ? `reason: ${reason}` : "";
  }
  if (normalized === "search_started") {
    if (query) return `query: ${query}`;
    if (queries.length) return `queries: ${queries.join(" | ")}`;
    return "";
  }
  if (normalized === "search_results") {
    const resultCount = payloadNumber(payload, ["result_count", "results_count", "num_results", "count"]);
    return resultCount !== null ? `${resultCount} results` : "";
  }
  if (normalized === "pages_read") {
    const pages = payloadNumber(payload, ["pages_read", "page_count", "documents_read"]);
    return pages !== null ? `${pages} pages` : "";
  }
  if (normalized === "gaps_identified") {
    const firstGap = payloadStringList(payload, ["gaps", "issues", "missing_points"])[0];
    return firstGap ? `gap: ${firstGap}` : "";
  }
  if (normalized === "retrieval_verification" || normalized === "answer_verification_completed") {
    const passed = payloadBool(payload, ["verified", "passed", "ok", "is_valid"]);
    if (passed === true) return "passed";
    if (passed === false) return "failed";
    return "";
  }
  if (normalized === "model_round_started" || normalized === "model_round_completed") {
    const model = payloadString(payload, ["model", "llm", "llm_used"]);
    const round = payloadString(payload, ["round", "round_id"]);
    return [round ? `round: ${round}` : "", model ? `model: ${model}` : ""].filter(Boolean).join(" | ");
  }
  if (normalized === "job_failed") {
    return reason ? `error: ${reason}` : "";
  }
  return "";
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
  const [searchQuery, setSearchQuery] = useState("");
  const [activeConversationId, setActiveConversationId] = useState("");
  const [inputValue, setInputValue] = useState("");
  const [loginError, setLoginError] = useState("");
  const [isLoadingLogin, setIsLoadingLogin] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [hasReceivedChunk, setHasReceivedChunk] = useState(false);
  const [deletingConversationId, setDeletingConversationId] = useState<string | null>(null);
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  const [isWorkLogOpen, setIsWorkLogOpen] = useState(false);

  const chatContainerRef = useRef<HTMLDivElement | null>(null);
  const messageEndRef = useRef<HTMLDivElement | null>(null);
  const shouldAutoScrollRef = useRef(true);
  const streamAbortControllerRef = useRef<AbortController | null>(null);

  const scrollToBottom = (behavior: ScrollBehavior, force = false) => {
    if (!force && !shouldAutoScrollRef.current) {
      return;
    }
    messageEndRef.current?.scrollIntoView({ behavior, block: "end" });
  };

  const handleChatScroll = () => {
    const container = chatContainerRef.current;
    if (!container) {
      return;
    }
    const threshold = 80;
    const distanceFromBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight;
    shouldAutoScrollRef.current = distanceFromBottom <= threshold;
  };

  useEffect(() => {
    document.documentElement.classList.toggle("dark", darkMode);
    localStorage.setItem(THEME_STORAGE_KEY, darkMode ? "dark" : "light");
  }, [darkMode]);

  useEffect(() => {
    localStorage.setItem(CHAT_MODE_STORAGE_KEY, chatMode);
  }, [chatMode]);

  useEffect(() => {
    scrollToBottom(isSending ? "auto" : "smooth", isSending);
  }, [messages, isSending]);

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

  const filteredConversations = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) return conversations;
    return conversations.filter(
      (item) =>
        item.title.toLowerCase().includes(query) ||
        item.prompt.toLowerCase().includes(query) ||
        item.answer.toLowerCase().includes(query)
    );
  }, [conversations, searchQuery]);

  const yourConversations = useMemo(() => filteredConversations.slice(0, 12), [filteredConversations]);

  const lastSevenDays = useMemo(() => {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    return filteredConversations.filter((item) => {
      const time = new Date(item.createdAt).getTime();
      return Number.isFinite(time) && time >= cutoff;
    });
  }, [filteredConversations]);

  const pipelineView = useMemo<PipelineView>(() => {
    const plannedQueries = new Set<string>();
    const subquestions = new Set<string>();
    const retrievalQueries = new Set<string>();
    const websites = new Set<string>();
    const sourceUrls = new Set<string>();
    const retrievalIssues = new Set<string>();
    const answerIssues = new Set<string>();
    const timeline: PipelineTimelineItem[] = [];

    let plannerType = "";
    let plannerLlm = "";
    let plannerSkippedReason = "";
    let retrievalVerificationPassed: boolean | null = null;
    let answerVerificationPassed: boolean | null = null;
    let finalStage: PipelineFinalStage = hasReceivedChunk ? "streaming" : "pending";
    let finalizedAt = "";

    workLogEvents.forEach((trace, index) => {
      const eventType = trace.type.trim().toLowerCase();
      const payload = trace.payload;
      for (const site of extractTraceWebsites(payload)) {
        websites.add(site);
      }
      for (const url of extractTraceUrls(payload)) {
        sourceUrls.add(url);
      }

      if (
        eventType === "query_plan_created" ||
        eventType === "query_planner_started" ||
        eventType === "query_planner_completed"
      ) {
        plannerType =
          plannerType || payloadString(payload, ["planner_type", "planner", "type", "strategy"]);
        plannerLlm =
          plannerLlm || payloadString(payload, ["llm_used", "planner_llm", "model", "llm"]);
        for (const query of payloadStringList(payload, ["planned_queries", "queries"])) {
          plannedQueries.add(query);
        }
        for (const question of payloadStringList(payload, ["subquestions", "sub_questions"])) {
          subquestions.add(question);
        }
      }

      if (eventType === "query_planner_skipped") {
        plannerSkippedReason =
          plannerSkippedReason || payloadString(payload, ["reason", "skip_reason", "message"]);
      }

      if (eventType === "search_started" || eventType === "search_results") {
        for (const query of payloadStringList(payload, ["query", "queries", "search_queries"])) {
          retrievalQueries.add(query);
        }
      }

      if (eventType === "retrieval_verification") {
        retrievalVerificationPassed = payloadBool(payload, ["verified", "passed", "ok", "is_valid"]);
        for (const issue of payloadStringList(payload, ["issues", "gaps", "problems"])) {
          retrievalIssues.add(issue);
        }
      }

      if (eventType === "gaps_identified") {
        for (const issue of payloadStringList(payload, ["gaps", "issues"])) {
          retrievalIssues.add(issue);
        }
      }

      if (eventType === "answer_verification_completed") {
        answerVerificationPassed = payloadBool(payload, ["verified", "passed", "ok", "is_valid"]);
        for (const issue of payloadStringList(payload, ["issues", "problems"])) {
          answerIssues.add(issue);
        }
      }

      if (eventType === "answer_finalized" || eventType === "job_completed") {
        finalStage = "finalized";
        finalizedAt = trace.timestamp ?? finalizedAt;
      }
      if (eventType === "job_failed") {
        finalStage = "failed";
      }

      timeline.push({
        key: `${trace.type}-${trace.timestamp ?? index}`,
        label: traceLabel(trace.type),
        detail: traceEventDetail(trace.type, trace.payload),
        timestamp: trace.timestamp,
        websites: extractTraceWebsites(trace.payload).slice(0, 3),
      });
    });

    if (hasReceivedChunk && finalStage === "pending") {
      finalStage = "streaming";
    }

    return {
      plannerType,
      plannerLlm,
      plannerSkippedReason,
      plannedQueries: Array.from(plannedQueries),
      subquestions: Array.from(subquestions),
      retrievalQueries: Array.from(retrievalQueries),
      websites: Array.from(websites),
      retrievalVerificationPassed,
      answerVerificationPassed,
      retrievalIssues: Array.from(retrievalIssues),
      answerIssues: Array.from(answerIssues),
      sourceUrls: Array.from(sourceUrls),
      finalStage,
      finalizedAt,
      timeline: timeline.slice(-80),
    };
  }, [hasReceivedChunk, workLogEvents]);

  const sourceWebsiteChips = useMemo(
    () => mergeUniqueStrings(pipelineView.websites, searchedWebsites, 20),
    [pipelineView.websites, searchedWebsites]
  );
  const showPipelinePanel = workLogEvents.length > 0 || pipelineView.sourceUrls.length > 0 || sourceWebsiteChips.length > 0;
  const workLogEventCount = pipelineView.timeline.length;
  const workedDurationLabel = useMemo(() => {
    const traceTimes = workLogEvents
      .map((event) => (event.timestamp ? new Date(event.timestamp).getTime() : Number.NaN))
      .filter((value) => Number.isFinite(value));
    if (!traceTimes.length) {
      return "0s";
    }
    const startedAt = Math.min(...traceTimes);
    let endedAt = Math.max(...traceTimes);
    if (pipelineView.finalizedAt) {
      const finalizedAtMs = new Date(pipelineView.finalizedAt).getTime();
      if (Number.isFinite(finalizedAtMs)) {
        endedAt = Math.max(endedAt, finalizedAtMs);
      }
    }
    if (pipelineView.finalStage === "pending" || pipelineView.finalStage === "streaming") {
      endedAt = Math.max(endedAt, Date.now());
    }
    return formatDurationLabel((endedAt - startedAt) / 1000);
  }, [pipelineView.finalStage, pipelineView.finalizedAt, workLogEvents]);
  const plannerSummary = pipelineView.plannerSkippedReason
    ? `Skipped: ${pipelineView.plannerSkippedReason}`
    : pipelineView.plannerType
      ? `${pipelineView.plannerType}${pipelineView.plannerLlm ? ` (${pipelineView.plannerLlm})` : ""}`
      : "Pending";
  const retrievalCheckStatus =
    pipelineView.retrievalVerificationPassed === true
      ? "Passed"
      : pipelineView.retrievalVerificationPassed === false
        ? "Failed"
        : "Pending";
  const answerCheckStatus =
    pipelineView.answerVerificationPassed === true
      ? "Passed"
      : pipelineView.answerVerificationPassed === false
        ? "Failed"
        : "Pending";
  const finalStatusLabel =
    pipelineView.finalStage === "finalized"
      ? "Finalized"
      : pipelineView.finalStage === "failed"
        ? "Failed"
        : pipelineView.finalStage === "streaming"
          ? "Streaming"
          : "Pending";
  const visitedWebsiteCount = useMemo(() => {
    const websites = new Set<string>();
    for (const url of pipelineView.sourceUrls) {
      websites.add(displayWebsite(url));
    }
    for (const site of sourceWebsiteChips) {
      websites.add(site);
    }
    return websites.size;
  }, [pipelineView.sourceUrls, sourceWebsiteChips]);
  const verificationIssueCount = pipelineView.retrievalIssues.length + pipelineView.answerIssues.length;

  const updateMessage = (messageId: string, updater: (message: ChatMessage) => ChatMessage) => {
    setMessages((prev) => prev.map((item) => (item.id === messageId ? updater(item) : item)));
  };

  const refreshHistory = async () => {
    if (!auth) return;
    const history = await fetchConversations(auth.userId, auth.token, 80);
    setConversations(history);
  };

  const sendPrompt = async (prompt: string, includeUserMessage = true) => {
    if (!auth || isSending) {
      return;
    }

    const cleanPrompt = prompt.trim();
    if (!cleanPrompt) {
      return;
    }

    setIsSending(true);
    setStatusError(false);
    setStatusText(includeUserMessage ? "Submitting prompt..." : "Regenerating response...");
    setCanCancelQueued(false);
    setIsAwaitingFirstChunk(true);
    setReasoningSteps([]);
    setSearchedWebsites([]);
    setWorkLogEvents([]);
    setIsWorkLogOpen(false);
    setActiveJobId("");
    setHasReceivedChunk(false);
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
      setReasoningSteps((prev) => mergeUniqueStrings(prev, filtered, 8));
    };
    const addSearchedWebsites = (websites: string[] | undefined) => {
      if (!websites?.length) {
        return;
      }
      const normalized = websites.map(displayWebsite);
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
      setWorkLogEvents((prev) => mergeTraceEvents(prev, [trace]));
      addReasoningSteps([traceLabel(trace.type)]);
      addSearchedWebsites(extractTraceWebsites(trace.payload));
      setStatusText(`Status: ${traceLabel(trace.type)}`);
      setStatusError(false);
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
          mode: chatMode,
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
              setHasReceivedChunk(true);
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
        updateMessage(assistantId, (item) => ({ ...item, content: "Request canceled." }));
        setActiveJobId("");
        localStorage.removeItem(ACTIVE_JOB_STORAGE_KEY);
        setStatusText("Request canceled.");
        setStatusError(false);
      } else {
        const message = error instanceof Error ? error.message : "Chat request failed.";
        updateMessage(assistantId, (item) => ({ ...item, content: `Error: ${message}` }));
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
    setIsWorkLogOpen(false);
    setActiveJobId("");
    setHasReceivedChunk(false);
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
        reaction: null,
      },
    ]);
    setStatusText(`Loaded conversation: ${conversation.title}`);
    setStatusError(false);
  };

  const handleRegenerate = async (sourcePrompt: string) => {
    await sendPrompt(sourcePrompt, false);
  };

  const handleReaction = (messageId: string, reaction: ReactionType) => {
    updateMessage(messageId, (item) => ({ ...item, reaction }));
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
    setIsWorkLogOpen(false);
    setActiveJobId("");
    setHasReceivedChunk(false);
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
        onNewChat={() => {
          setMessages([]);
          setActiveConversationId("");
          setReasoningSteps([]);
          setSearchedWebsites([]);
          setWorkLogEvents([]);
          setIsWorkLogOpen(false);
          setActiveJobId("");
          setHasReceivedChunk(false);
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
              {canCancelQueued ? (
                <button
                  type="button"
                  onClick={handleCancelQueuedRequest}
                  className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-700 hover:bg-amber-100 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200 dark:hover:bg-amber-900/40"
                >
                  Cancel
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
          <div className="mx-auto flex min-h-full w-full max-w-4xl flex-col gap-6 px-4 pb-44 pt-8">
            {showPipelinePanel ? (
              <section className="rounded-2xl border border-blue-200 bg-white/80 p-4 shadow-soft dark:border-slate-700 dark:bg-slate-900/70">
                <button
                  type="button"
                  onClick={() => setIsWorkLogOpen((prev) => !prev)}
                  className="flex w-full items-center gap-3 text-left"
                >
                  <span className="h-px flex-1 bg-blue-100 dark:bg-slate-700" />
                  <span className="inline-flex items-center gap-2 rounded-full border border-blue-200 bg-white px-4 py-1.5 text-base font-medium text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200">
                    Worked for {workedDurationLabel}
                    <span aria-hidden="true" className="text-base leading-none text-slate-500 dark:text-slate-300">
                      {isWorkLogOpen ? "▾" : "▸"}
                    </span>
                  </span>
                  <span className="h-px flex-1 bg-blue-100 dark:bg-slate-700" />
                </button>

                {isWorkLogOpen ? (
                  <div className="mt-3 max-h-[44vh] space-y-3 overflow-y-auto pr-1">
                    <div className="grid gap-2 sm:grid-cols-2">
                      <article className="rounded-xl border border-blue-100 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                          Planner
                        </p>
                        <p className="mt-1 text-sm text-slate-700 dark:text-slate-200">{plannerSummary}</p>
                      </article>
                      <article className="rounded-xl border border-blue-100 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                          Retrieval
                        </p>
                        <p className="mt-1 text-sm text-slate-700 dark:text-slate-200">
                          {pipelineView.retrievalQueries.length} queries, {visitedWebsiteCount} websites
                        </p>
                      </article>
                      <article className="rounded-xl border border-blue-100 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                          Verification
                        </p>
                        <p className="mt-1 text-sm text-slate-700 dark:text-slate-200">
                          Retrieval {retrievalCheckStatus}, Answer {answerCheckStatus}
                        </p>
                        {verificationIssueCount ? (
                          <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                            {verificationIssueCount} issues flagged
                          </p>
                        ) : null}
                      </article>
                      <article className="rounded-xl border border-blue-100 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                          Final
                        </p>
                        <p className="mt-1 text-sm text-slate-700 dark:text-slate-200">{finalStatusLabel}</p>
                        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                          {workLogEventCount} events
                          {pipelineView.finalizedAt ? ` · ${traceTimeLabel(pipelineView.finalizedAt)}` : ""}
                        </p>
                      </article>
                    </div>

                    {pipelineView.retrievalQueries.length ? (
                      <div className="rounded-xl border border-blue-100 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                          Top queries
                        </p>
                        <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-slate-700 dark:text-slate-200">
                          {pipelineView.retrievalQueries.slice(0, 3).map((query) => (
                            <li key={query}>{query}</li>
                          ))}
                        </ul>
                      </div>
                    ) : null}

                    {sourceWebsiteChips.length ? (
                      <div className="rounded-xl border border-blue-100 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                          Websites searched
                        </p>
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          {sourceWebsiteChips.slice(0, 8).map((site) => (
                            <code
                              key={site}
                              className="rounded bg-blue-50 px-1.5 py-0.5 text-[11px] text-blue-700 dark:bg-slate-800 dark:text-slate-200"
                            >
                              {site}
                            </code>
                          ))}
                        </div>
                      </div>
                    ) : null}

                    {pipelineView.sourceUrls.length ? (
                      <div className="rounded-xl border border-blue-100 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                        <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                          Key sources
                        </p>
                        <ul className="mt-1 space-y-1 text-sm text-slate-700 dark:text-slate-200">
                          {pipelineView.sourceUrls.slice(0, 3).map((url) => (
                            <li key={`source-${url}`}>
                              <a
                                href={url}
                                target="_blank"
                                rel="noreferrer"
                                className="break-all text-blue-700 hover:underline dark:text-blue-300"
                              >
                                {url}
                              </a>
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </section>
            ) : null}

            {messages.length ? (
              messages.map((message) => (
                <MessageCard
                  key={message.id}
                  message={message}
                  onRegenerate={handleRegenerate}
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

        <div className="fixed bottom-0 left-0 right-0 border-t border-blue-100 bg-white/75 backdrop-blur-md dark:border-slate-800 dark:bg-slate-950/80 md:left-[260px]">
          <ChatInput
            value={inputValue}
            disabled={isSending}
            mode={chatMode}
            onChange={setInputValue}
            onModeChange={setChatMode}
            onSubmit={async () => {
              await sendPrompt(inputValue, true);
            }}
          />
        </div>
      </div>
    </div>
  );
}
