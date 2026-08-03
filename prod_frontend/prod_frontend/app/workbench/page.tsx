"use client";

import Link from "next/link";
import { useState } from "react";
import { decomposeApi, DecomposeResponse } from "@/lib/api";
import { opsToGraph } from "@/lib/opsToGraph";
import WorkflowGraph from "@/components/WorkflowGraph";

export default function WorkbenchPage() {
  const [problem, setProblem] = useState("");
  const [result, setResult] = useState<DecomposeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const [decided, setDecided] = useState<string | null>(null);
  const [approverId, setApproverId] = useState("");

  async function submit() {
    const text = problem.trim();
    if (!text || working) return;

    setWorking(true);
    setError(null);
    setResult(null);
    setDecided(null);
    try {
      setResult(await decomposeApi.submit(text));
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Could not reach the decomposition service.",
      );
    } finally {
      setWorking(false);
    }
  }

  async function decide(decision: "approved" | "rejected") {
    if (!result || !approverId.trim()) {
      alert("Enter who's making this decision first.");
      return;
    }
    setWorking(true);
    try {
      const response = await decomposeApi.decide(result.id, approverId, decision);
      setDecided(response.decision);
    } catch (e) {
      alert(e instanceof Error ? e.message : "Could not record the decision.");
    } finally {
      setWorking(false);
    }
  }

  const graph = result ? opsToGraph(result.ops) : null;

  return (
    <main className="shell">
      <div className="masthead">
        <div>
          <div className="masthead-title">Workbench</div>
          <div className="masthead-sub">describe a problem, get a task breakdown</div>
        </div>
      </div>

      <div className="nav-tabs">
        <Link href="/workbench" className="nav-tab active">
          Workbench
        </Link>
        <Link href="/approvals" className="nav-tab">
          Docket
        </Link>
        <Link href="/archive" className="nav-tab">
          Archive
        </Link>
        <Link href="/agents/medical-report-extraction" className="nav-tab">
          Agents
        </Link>
      </div>

      <textarea
        className="problem-input"
        value={problem}
        placeholder="e.g. We receive client PDFs each month and need summary charts from the tables inside them."
        onChange={(e) => setProblem(e.target.value)}
        rows={5}
        disabled={working}
      />
      <div className="ask-bar">
        <button
          className="ask-button"
          onClick={submit}
          disabled={working || !problem.trim()}
        >
          {working ? "Working…" : "Decompose"}
        </button>
      </div>

      {error && <div className="empty-state">{error}</div>}

      {result && (
        <div className="case-file" style={{ marginTop: "2rem" }}>
          {/* Manipulation suspicion goes first — a reviewer who reads the
              plan before the warning has already been influenced by it. */}
          {(result.suspected_manipulation || result.input_flagged) && (
            <div className="tier-banner simulated">
              {result.suspected_manipulation
                ? "This submission may have attempted to manipulate the system"
                : "This submission matched known manipulation patterns"}
              <span className="tier-detail">
                Read the proposed steps for anything that came from instructions
                rather than from the problem itself.
              </span>
            </div>
          )}

          {!result.feasible ? (
            <>
              <h2 className="case-heading">No workflow could be derived</h2>
              <p className="case-body">{result.reasoning}</p>
            </>
          ) : (
            <>
              <h2 className="case-heading">
                {result.node_count} step{result.node_count === 1 ? "" : "s"} proposed
              </h2>

              <div className="case-section">
                <div className="case-label">Reasoning</div>
                <p className="case-body">{result.reasoning}</p>
              </div>

              {graph && graph.nodes.length > 0 && (
                <div className="case-section">
                  <div className="case-label">Proposed workflow</div>
                  <WorkflowGraph
                    nodes={graph.nodes}
                    edges={graph.edges}
                    center={graph.nodes[0].id}
                  />
                </div>
              )}

              {result.structural_problems.length > 0 && (
                <div className="case-section">
                  <div className="case-label">Blocked — cannot be proposed</div>
                  {result.structural_problems.map((p, i) => (
                    <div key={i} className="objection">
                      {p}
                    </div>
                  ))}
                </div>
              )}

              {result.objections.length > 0 && (
                <div className="case-section">
                  <div className="case-label">Raised in adversarial review</div>
                  <ul className="evidence-notes">
                    {result.objections.map((o, i) => (
                      <li key={i}>{o}</li>
                    ))}
                  </ul>
                </div>
              )}

              {result.related_existing.length > 0 && (
                <div className="case-section">
                  <div className="case-label">Existing steps that may already do this</div>
                  <ul className="evidence-notes">
                    {result.related_existing.map((r, i) => (
                      <li key={i}>{r}</li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="case-section">
                <div className="case-label">Status</div>
                <p className="case-body">
                  This is a proposal. Nothing has been added to the shared
                  library — an approval is required first, and approved content
                  stays marked as generated from a public submission.
                </p>
              </div>

              {decided ? (
                <div className={`stamp ${decided}`}>{decided}</div>
              ) : (
                result.safe_to_propose && (
                  <div className="case-section">
                    <div className="case-label">Decision</div>
                    <input
                      className="ask-input"
                      style={{ width: "100%", marginBottom: "0.75rem" }}
                      placeholder="Your name or id"
                      value={approverId}
                      onChange={(e) => setApproverId(e.target.value)}
                    />
                    <div className="ruling-bar">
                      <button
                        className="ruling-button approve"
                        disabled={working}
                        onClick={() => decide("approved")}
                      >
                        Add to library
                      </button>
                      <button
                        className="ruling-button reject"
                        disabled={working}
                        onClick={() => decide("rejected")}
                      >
                        Discard
                      </button>
                    </div>
                  </div>
                )
              )}
            </>
          )}
        </div>
      )}
    </main>
  );
}
