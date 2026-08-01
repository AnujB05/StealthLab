"use client";

import { Layer2Metrics } from "@/lib/api";

/**
 * Renders Layer 2 empirical results.
 *
 * The single most important thing this component does is make the
 * *evidence tier* impossible to miss. A confidence interval and a p-value
 * look identical whether they came from a shadow deployment or from a
 * language model guessing at a counterfactual — and the second is close
 * to worthless as positive evidence. An approver reading "success rate
 * +0.80, p=0.0002" will act on it unless the page tells them plainly what
 * produced that number.
 *
 * Hence: the tier banner sits above the numbers, not below them, and
 * simulated evidence is styled as a caution rather than a result.
 */

const METRIC_LABELS: Record<string, string> = {
  success_rate: "Success rate",
  rework_rate: "Rework rate",
  latency_ms: "Latency (ms)",
  cost: "Cost",
};

const VERDICT_LABELS: Record<string, string> = {
  better: "improvement",
  worse: "regression",
  no_detectable_difference: "no detectable difference",
  inconclusive: "inconclusive",
};

function formatDelta(metric: string, delta: number): string {
  const sign = delta > 0 ? "+" : "";
  if (metric === "success_rate" || metric === "rework_rate") {
    return `${sign}${(delta * 100).toFixed(1)} pts`;
  }
  if (metric === "cost") return `${sign}${delta.toFixed(4)}`;
  return `${sign}${delta.toFixed(1)}`;
}

export default function Layer2Evidence({ metrics }: { metrics: Layer2Metrics }) {
  const simulated = metrics.tier === 3;

  return (
    <div>
      <div className={`tier-banner${simulated ? " simulated" : ""}`}>
        {simulated ? "Simulated evidence — not measured" : "Observed evidence"}
        <span className="tier-detail">
          {metrics.tier_label} · n={metrics.n_observations}
        </span>
      </div>

      {!metrics.sufficient_data ? (
        <p className="case-body" style={{ fontStyle: "italic" }}>
          Not enough data to make a statistical comparison.
        </p>
      ) : (
        <table className="metrics-table">
          <thead>
            <tr>
              <th>Metric</th>
              <th>Baseline</th>
              <th>Candidate</th>
              <th>Change</th>
              <th>95% CI</th>
              <th>Verdict</th>
            </tr>
          </thead>
          <tbody>
            {metrics.comparisons.map((c) => (
              <tr key={c.metric}>
                <td>{METRIC_LABELS[c.metric] ?? c.metric}</td>
                <td>{c.baseline_mean.toFixed(3)}</td>
                <td>{c.candidate_mean.toFixed(3)}</td>
                <td
                  className={
                    c.verdict === "better"
                      ? "delta-better"
                      : c.verdict === "worse"
                        ? "delta-worse"
                        : ""
                  }
                >
                  {formatDelta(c.metric, c.delta)}
                </td>
                <td className="ci-cell">
                  [{c.ci_lower.toFixed(3)}, {c.ci_upper.toFixed(3)}]
                </td>
                <td>{VERDICT_LABELS[c.verdict] ?? c.verdict}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {metrics.notes.length > 0 && (
        <ul className="evidence-notes">
          {metrics.notes.map((note, i) => (
            <li key={i}>{note}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
