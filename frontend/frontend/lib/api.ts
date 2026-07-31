/**
 * Typed client for the backend API. Every shape here mirrors a real
 * Pydantic model or SQL row from the backend, not a guess — see
 * app/models/debate.py, app/api/approval.py in the backend for the
 * source of truth. If the backend schema changes, this is the one file
 * to update.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type FallacyFlag = {
  fallacy: string;
  quote: string;
  explanation: string;
};

export type ScorecardSummary = {
  id: string;
  debate_id: string;
  candidate_id: string;
  layer1_passed: boolean;
  groundedness_score: number;
  blast_radius: number;
  reversible: boolean;
  recommendation: string;
  summary: string;
  supporters: string[];
  created_at: string;
};

export type DebateTurn = {
  round_number: number;
  speaker_id: string;
  speaker_kind: "agent" | "human";
  model_used: string | null;
  action: "propose" | "amend" | "pass";
  candidate_id: string | null;
  content: string;
  cites: { node_id: string; node_table: string }[];
  created_at: string;
};

export type ScorecardDetail = ScorecardSummary & {
  fallacy_flags: FallacyFlag[];
  constructive: boolean;
  unresolved_cites: string[];
  rationale: string;
  change_set: { ops: Record<string, unknown>[] };
  round_number: number;
  termination_reason: string;
  transcript: DebateTurn[];
};

export type ApprovalResponse = {
  approval_id: string;
  decision: "approved" | "rejected";
  applied_ops: Record<string, unknown>[];
  export_markdown: string | null;
};

export type DebateOutcome = {
  debate_id: string;
  state: string;
  termination_reason: string | null;
  candidates_proposed: number;
  candidates_passed_layer1: number;
  detail: string | null;
};

export type ScanResponse = {
  triggers_found: number;
  debates_run: number;
  outcomes: DebateOutcome[];
  errors: string[];
};

class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text();
    throw new ApiError(body || res.statusText, res.status);
  }
  return res.json() as Promise<T>;
}

export const api = {
  listPending: () => request<ScorecardSummary[]>("/v1/approvals/pending"),

  getDetail: (scorecardId: string) =>
    request<ScorecardDetail>(`/v1/approvals/${scorecardId}`),

  decide: (
    scorecardId: string,
    body: { approver_id: string; approver_role?: string; decision: "approved" | "rejected"; note?: string },
  ) =>
    request<ApprovalResponse>(`/v1/approvals/${scorecardId}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  runScan: () => request<ScanResponse>("/v1/admin/scan", { method: "POST" }),
};

export { ApiError };
