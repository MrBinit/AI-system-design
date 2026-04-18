export type ChatRole = "user" | "assistant";
export type ReactionType = "like" | "dislike" | null;
export type ChatExecutionMode = "auto" | "fast" | "standard" | "deep";

export interface AuthSession {
  token: string;
  userId: string;
  username: string;
  roles: string[];
  sessionId: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  roles: string[];
  expires_in_seconds: number;
}

export interface ConversationItem {
  conversationId: string;
  title: string;
  prompt: string;
  answer: string;
  createdAt: string;
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  username: string;
  content: string;
  createdAt: string;
  sourcePrompt?: string;
  executionMode?: ChatExecutionMode;
  workedForLabel?: string;
  sourceUrls?: string[];
  reasoningSteps?: string[];
  searchedWebsites?: string[];
  traceEvents?: TraceEventItem[];
  trustConfidence?: number;
  trustFreshness?: string;
  trustContradiction?: boolean;
  claimCitationCoverage?: number;
  uncertaintyReasons?: string[];
  reaction?: ReactionType;
}

export interface TraceEventItem {
  type: string;
  timestamp?: string;
  payload?: Record<string, unknown>;
}

export interface StreamEvent {
  type: "queued" | "status" | "chunk" | "done" | "error" | "search" | "reasoning" | "trace";
  status?: string;
  job_id?: string;
  text?: string;
  delta?: string;
  token?: string;
  content?: string;
  message?: string;
  detail?: string;
  websites?: string[];
  steps?: string[];
  trace?: TraceEventItem;
}
