"use client";

import { ReactNode } from "react";

/**
 * Renders a grounded answer, converting inline `[task:<uuid>]` /
 * `[knowledge:<uuid>]` markers into numbered superscripts.
 *
 * The markers carry real information, so they aren't just stripped:
 * a citation the backend could not resolve against the graph is a
 * *hallucinated* reference, and the reader needs to see which specific
 * claim rests on it — not just a count in a footer.
 *
 * Verification status comes from the backend, never inferred here.
 * Anything not explicitly confirmed resolved is rendered as unverified:
 * over-flagging is a minor annoyance, while falsely presenting an
 * invented citation as verified is the failure this whole layer exists
 * to prevent.
 */

const CITE = /\[(task|knowledge):([0-9a-fA-F-]{36})\]/g;

export default function GroundedAnswer({
  text,
  citedNodeIds,
  unresolvedCitations,
}: {
  text: string;
  citedNodeIds: string[];
  unresolvedCitations: string[];
}) {
  const resolved = new Set(citedNodeIds.map((id) => id.toLowerCase()));
  const unresolved = new Set(unresolvedCitations.map((c) => c.toLowerCase()));

  const parts: ReactNode[] = [];
  let lastIndex = 0;
  let citationNumber = 0;
  let match: RegExpExecArray | null;

  CITE.lastIndex = 0;
  while ((match = CITE.exec(text)) !== null) {
    const [full, kind, rawId] = match;
    const id = rawId.toLowerCase();

    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }

    citationNumber += 1;
    const isResolved = resolved.has(id);
    const isKnownBad = unresolved.has(`${kind}:${id}`);
    // Conservative default: only mark verified when the backend
    // explicitly confirmed it.
    const verified = isResolved && !isKnownBad;

    parts.push(
      <sup
        key={`${match.index}-${rawId}`}
        className={`cite-marker${verified ? "" : " unresolved"}`}
        title={
          verified
            ? `Verified against ${kind} node ${rawId}`
            : `Could not verify ${kind} node ${rawId} — this reference may be invented`
        }
      >
        {citationNumber}
      </sup>,
    );

    lastIndex = match.index + full.length;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return <>{parts}</>;
}
