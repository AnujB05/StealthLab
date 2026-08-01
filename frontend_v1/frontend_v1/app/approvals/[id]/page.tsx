"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api, ScorecardDetail, ApprovalResponse, SubgraphResponse } from "@/lib/api";
import WorkflowGraph from "@/components/WorkflowGraph";
import Layer2Evidence from "@/components/Layer2Evidence";

export default function CaseFile() {
  const params = useParams<{ id: string }>();
  const [detail, setDetail] = useState<ScorecardDetail | null>(null);
  const [subgraph, setSubgraph] = useState<SubgraphResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deciding, setDeciding] = useState(false);
  const [result, setResult] = useState<ApprovalResponse | null>(null);
  const [approverId, setApproverId] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await api.getDetail(params.id);
      setDetail(d);
      // The graph is supplementary context, not the point of the page --
      // if it fails, the case file should still be fully reviewable.
      if (d.task_node_id) {
        try {
          setSubgraph(await api.getSubgraph(d.task_node_id));
        } catch {
          setSubgraph(null);
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load this case.");
    }
  }, [params.id]);

  useEffect(() => {
    load();
  }, [load]);

  async function decide(decision: "approved" | "rejected") {
    if (!approverId.trim()) {
      alert("Enter who's making this decision before ruling.");
      return;
    }
    setDeciding(true);
    try {
      const res = await api.decide(params.id, { approver_id: approverId, decision });
      setResult(res);
    } catch (e) {
      alert(e instanceof Error ? e.message : "Could not record the decision.");
    } finally {
      setDeciding(false);
    }
  }

  if (error) {
    return (
      <main className="shell">
        <Link href="/approvals" className="back-link">
          ← back to docket
        </Link>
        <div className="empty-state">{error}</div>
      </main>
    );
  }

  if (!detail) {
    return (
      <main className="shell">
        <p style={{ color: "var(--ink-text-dim)" }}>Loading…</p>
      </main>
    );
  }

  return (
    <main className="shell">
      <Link href="/approvals" className="back-link">
        ← back to docket
      </Link>

      <div className="case-file">
        <h1 className="case-heading">{detail.summary}</h1>

        <div className="case-section">
          <div className="case-label">Metrics</div>
          <div className="metrics-row">
            <div className="metric">
              <span className="metric-value">{detail.groundedness_score.toFixed(2)}</span>
              <span className="metric-label">groundedness</span>
            </div>
            <div className="metric">
              <span className="metric-value">{detail.blast_radius}</span>
              <span className="metric-label">dependent tasks</span>
            </div>
            <div className="metric">
              <span className="metric-value">{detail.round_number}</span>
              <span className="metric-label">debate rounds ({detail.termination_reason})</span>
            </div>
          </div>
        </div>

        {subgraph && subgraph.nodes.length > 0 && (
          <div className="case-section">
            <div className="case-label">
              Workflow context — the task under review, outlined
            </div>
            <WorkflowGraph
              nodes={subgraph.nodes}
              edges={subgraph.edges}
              center={subgraph.center}
            />
          </div>
        )}

        <div className="case-section">
          <div className="case-label">Argument · proposed by {detail.supporters.join(", ")}</div>
          <p className="case-body">{detail.rationale}</p>
        </div>

        {detail.fallacy_flags.length > 0 && (
          <div className="case-section">
            <div className="case-label">Objections raised in review</div>
            {detail.fallacy_flags.map((f, i) => (
              <div key={i} className="objection">
                <div className="objection-fallacy">{f.fallacy}</div>
                <div className="objection-quote">&ldquo;{f.quote}&rdquo;</div>
                <div>{f.explanation}</div>
              </div>
            ))}
          </div>
        )}

        {!detail.constructive && (
          <div className="case-section">
            <div className="objection">
              Flagged as non-constructive — this candidate criticizes without proposing an
              alternative.
            </div>
          </div>
        )}

        <div className="case-section">
          <div className="case-label">Proposed changes</div>
          {detail.change_set.ops.length === 0 ? (
            <p className="case-body" style={{ fontStyle: "italic" }}>
              No changes specified.
            </p>
          ) : (
            detail.change_set.ops.map((op, i) => (
              <pre key={i} className="change-op">
                {JSON.stringify(op, null, 2)}
              </pre>
            ))
          )}
        </div>

        <div className="case-section">
          <div className="case-label">Empirical evidence</div>
          {detail.layer2_metrics ? (
            <Layer2Evidence metrics={detail.layer2_metrics} />
          ) : (
            <p className="case-body" style={{ fontStyle: "italic" }}>
              No empirical testing has been run on this candidate — the review
              above assesses the argument only, not the outcome.
            </p>
          )}
        </div>

        <div className="case-section">
          <div className="case-label">Recommendation — advisory only</div>
          <p className="case-body">{detail.recommendation}</p>
        </div>

        {detail.transcript.length > 0 && (
          <div className="case-section">
            <div className="case-label">Debate transcript</div>
            {detail.transcript.map((turn, i) => (
              <div key={i} className="transcript-turn">
                <div>
                  <div className="transcript-speaker">
                    {turn.speaker_id}
                    {turn.model_used && <> · {turn.model_used}</>}
                  </div>
                  <span className="transcript-action">{turn.action}</span>
                </div>
                <div className="transcript-content">{turn.content}</div>
              </div>
            ))}
          </div>
        )}

        {result ? (
          <div className={`stamp ${result.decision}`}>{result.decision}</div>
        ) : (
          <div className="case-section">
            <div className="case-label">Your decision</div>
            <input
              type="text"
              placeholder="Your name or id"
              value={approverId}
              onChange={(e) => setApproverId(e.target.value)}
              style={{
                width: "100%",
                padding: "0.6rem 0.8rem",
                marginBottom: "0.75rem",
                fontFamily: "var(--font-mono)",
                fontSize: "0.85rem",
                border: "1px solid var(--rule-paper)",
                borderRadius: "3px",
                background: "var(--paper-dim)",
                color: "var(--paper-text)",
              }}
            />
            <div className="ruling-bar">
              <button
                className="ruling-button approve"
                disabled={deciding}
                onClick={() => decide("approved")}
              >
                Approve
              </button>
              <button
                className="ruling-button reject"
                disabled={deciding}
                onClick={() => decide("rejected")}
              >
                Reject
              </button>
            </div>
          </div>
        )}

        {result?.export_markdown && (
          <div className="case-section">
            <div className="case-label">Export</div>
            <pre className="change-op">{result.export_markdown}</pre>
          </div>
        )}
      </div>
    </main>
  );
}
