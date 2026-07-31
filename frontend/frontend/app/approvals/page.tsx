"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ScorecardSummary } from "@/lib/api";

export default function ApprovalsDocket() {
  const [items, setItems] = useState<ScorecardSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanMessage, setScanMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await api.listPending());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the API.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleScan() {
    setScanning(true);
    setScanMessage(null);
    try {
      const result = await api.runScan();
      if (result.triggers_found === 0) {
        setScanMessage("No bottlenecks crossed the current thresholds.");
      } else {
        const failed = result.outcomes.filter((o) => o.candidates_proposed === 0);
        if (failed.length > 0 && failed[0].detail) {
          // Something real happened, but no candidate survived to review —
          // show why rather than a generic "completed" that would hide a
          // billing/auth failure behind a falsely reassuring message.
          setScanMessage(`Debate ran but produced nothing to review: ${failed[0].detail}`);
        } else {
          const passed = result.outcomes.reduce((n, o) => n + o.candidates_passed_layer1, 0);
          setScanMessage(
            `${result.triggers_found} trigger(s) found, ${result.debates_run} debate(s) run, ` +
              `${passed} candidate(s) ready for review.`,
          );
        }
      }
      await load();
    } catch (e) {
      setScanMessage(
        e instanceof Error
          ? `Scan failed: ${e.message}`
          : "Scan failed. Check that API keys are configured in the backend .env.",
      );
    } finally {
      setScanning(false);
    }
  }

  return (
    <main className="shell">
      <div className="masthead">
        <div>
          <div className="masthead-title">Open docket</div>
          <div className="masthead-sub">workflow changes awaiting review</div>
        </div>
        <button className="scan-button" onClick={handleScan} disabled={scanning}>
          {scanning ? "Scanning…" : "Run scan"}
        </button>
      </div>

      {scanMessage && (
        <p style={{ fontFamily: "var(--font-mono)", fontSize: "0.8rem", color: "var(--ink-text-dim)" }}>
          {scanMessage}
        </p>
      )}

      {error && (
        <div className="empty-state">
          Could not reach the API at the configured address.
          <br />
          Check NEXT_PUBLIC_API_BASE_URL and that the backend is running.
        </div>
      )}

      {!error && items === null && <p style={{ color: "var(--ink-text-dim)" }}>Loading…</p>}

      {!error && items?.length === 0 && (
        <div className="empty-state">
          Nothing open. Run a scan, or wait for the next scheduled one.
        </div>
      )}

      {items?.map((item) => (
        <Link
          key={item.id}
          href={`/approvals/${item.id}`}
          className={`docket-item ${item.layer1_passed ? "clean" : "flagged"}`}
        >
          <div className="docket-eyebrow">
            {item.layer1_passed ? "argument review passed" : "argument review flagged"} · proposed by{" "}
            {item.supporters.join(", ")}
          </div>
          <p className="docket-summary">{item.summary}</p>
          <div className="docket-meta">
            <span>groundedness {item.groundedness_score.toFixed(2)}</span>
            <span>blast radius {item.blast_radius}</span>
            <span>{item.reversible ? "reversible" : "not reversible"}</span>
          </div>
        </Link>
      ))}
    </main>
  );
}
