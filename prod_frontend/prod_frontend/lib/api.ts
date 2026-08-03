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

export type Layer2Comparison = {
  metric: string;
  baseline_mean: number;
  candidate_mean: number;
  delta: number;
  ci_lower: number;
  ci_upper: number;
  p_value: number;
  verdict: "better" | "worse" | "no_detectable_difference" | "inconclusive";
  n: number;
};

export type Layer2Metrics = {
  tier: number;
  tier_label: string;
  n_observations: number;
  sufficient_data: boolean;
  notes: string[];
  comparisons: Layer2Comparison[];
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
  task_node_id: string;
  layer2_tier: number | null;
  layer2_metrics: Layer2Metrics | null;
  value_delivered: number | null;
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

export type GraphNode = {
  id: string;
  table: "knowledge_nodes" | "task_nodes";
  label: string;
};

export type GraphEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
};

export type SubgraphResponse = {
  center: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
};

export type ChatResponse = {
  answer: string;
  cited_node_ids: string[];
  unresolved_citations: string[];
  groundedness: number;
  retrieved_count: number;
  context_empty: boolean;
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

async function requestMultipart<T>(path: string, formData: FormData): Promise<T> {
  // Deliberately does NOT set Content-Type -- the browser must set it
  // itself for multipart/form-data, including a boundary string it
  // generates, which JavaScript cannot construct correctly by hand.
  // Setting "application/json" here (request()'s default) would silently
  // corrupt the upload rather than fail loudly.
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    body: formData,
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text();
    throw new ApiError(body || res.statusText, res.status);
  }
  return res.json() as Promise<T>;
}

export type ExtractionStatus = {
  original_filename: string;
  outcome: "success" | "failure";
  field_count: number | null;
  error: string | null;
};

export type RunResponse = {
  extractions: ExtractionStatus[];
  combined_file_id: string | null;
  combined_download_path: string | null;
  combined_error: string | null;
};

export const agentsApi = {
  runMedicalReportExtraction: (files: File[]) => {
    const formData = new FormData();
    for (const file of files) formData.append("files", file);
    return requestMultipart<RunResponse>(
      "/v1/agents/medical-report-extraction/run",
      formData,
    );
  },

  downloadUrl: (downloadPath: string) => `${API_BASE}${downloadPath}`,
};

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

  getSubgraph: (nodeId: string, depth = 2) =>
    request<SubgraphResponse>(`/v1/graph/${nodeId}?depth=${depth}`),

  ask: (question: string, topK = 6) =>
    request<ChatResponse>("/v1/chat", {
      method: "POST",
      body: JSON.stringify({ question, top_k: topK }),
    }),
};

export { ApiError };

// --- V2 Tab 1: decomposition ---

export type DecomposeResponse = {
  id: string;
  feasible: boolean;
  reasoning: string;
  ops: Record<string, unknown>[];
  node_count: number;
  safe_to_propose: boolean;
  structural_problems: string[];
  objections: string[];
  suspected_manipulation: boolean;
  input_flagged: boolean;
  input_truncated: boolean;
  related_existing: string[];
};

export type DecideResponse = {
  id: string;
  decision: "approved" | "rejected";
  created_nodes: Record<string, unknown>[];
  refs: Record<string, string>;
};

export const decomposeApi = {
  submit: (problem: string) =>
    request<DecomposeResponse>("/v1/decompose", {
      method: "POST",
      body: JSON.stringify({ problem }),
    }),

  decide: (id: string, approverId: string, decision: "approved" | "rejected") =>
    request<DecideResponse>(`/v1/decompose/${id}/decide`, {
      method: "POST",
      body: JSON.stringify({ approver_id: approverId, decision }),
    }),
};
