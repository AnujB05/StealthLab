"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { agentsApi, ExtractionStatus, RunResponse } from "@/lib/api";

export default function MedicalReportExtractionAgent() {
  const [files, setFiles] = useState<File[]>([]);
  const [running, setRunning] = useState(false);
  const [results, setResults] = useState<RunResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  function addFiles(newFiles: FileList | null) {
    if (!newFiles) return;
    // Append rather than replace -- uploading in two batches shouldn't
    // silently drop the first one.
    setFiles((prev) => [...prev, ...Array.from(newFiles)]);
  }

  function removeFile(index: number) {
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }

  async function run() {
    if (files.length === 0 || running) return;
    setRunning(true);
    setError(null);
    setResults(null);
    try {
      const response = await agentsApi.runMedicalReportExtraction(files);
      setResults(response);
    } catch (e) {
      setError(
        e instanceof Error
          ? e.message
          : "Could not reach the agent service.",
      );
    } finally {
      setRunning(false);
    }
  }

  function reset() {
    setFiles([]);
    setResults(null);
    setError(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  return (
    <main className="shell">
      <div className="masthead">
        <div>
          <div className="masthead-title">Medical Report Extraction</div>
          <div className="masthead-sub">
            upload one or more lab report PDFs, get structured Excel back
          </div>
        </div>
      </div>

      <div className="nav-tabs">
        <Link href="/workbench" className="nav-tab">
          Workbench
        </Link>
        <Link href="/approvals" className="nav-tab">
          Docket
        </Link>
        <Link href="/archive" className="nav-tab">
          Archive
        </Link>
        <Link href="/agents/medical-report-extraction" className="nav-tab active">
          Agents
        </Link>
      </div>

      {!results && (
        <>
          <div className="upload-dropzone">
            <input
              ref={inputRef}
              type="file"
              accept="application/pdf"
              multiple
              onChange={(e) => addFiles(e.target.files)}
              disabled={running}
              id="pdf-upload"
              style={{ display: "none" }}
            />
            <label htmlFor="pdf-upload" className="upload-label">
              {files.length === 0
                ? "Click to choose PDF report(s), or drop them here"
                : "Add more files"}
            </label>
          </div>

          {files.length > 0 && (
            <ul className="upload-file-list">
              {files.map((f, i) => (
                <li key={i}>
                  <span>{f.name}</span>
                  <span className="upload-file-size">
                    {(f.size / 1024).toFixed(0)} KB
                  </span>
                  <button
                    className="upload-remove"
                    onClick={() => removeFile(i)}
                    disabled={running}
                    aria-label={`Remove ${f.name}`}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="ask-bar">
            <button
              className="ask-button"
              onClick={run}
              disabled={files.length === 0 || running}
            >
              {running
                ? `Processing ${files.length} file${files.length === 1 ? "" : "s"}…`
                : `Run on ${files.length} file${files.length === 1 ? "" : "s"}`}
            </button>
          </div>

          {error && <div className="empty-state">{error}</div>}
        </>
      )}

      {results && (
        <div className="case-file">
          <h2 className="case-heading">
            {results.extractions.filter((r) => r.outcome === "success").length} of{" "}
            {results.extractions.length} extracted successfully
          </h2>

          <ul className="agent-results-list">
            {results.extractions.map((r: ExtractionStatus, i: number) => (
              <li key={i} className="agent-result-row">
                <span className="agent-result-name">{r.original_filename}</span>
                {r.outcome === "success" ? (
                  <span className="agent-result-meta">
                    {r.field_count} field{r.field_count === 1 ? "" : "s"}
                  </span>
                ) : (
                  <span className="agent-result-error">{r.error}</span>
                )}
              </li>
            ))}
          </ul>

          <div className="case-section">
            <div className="case-label">Combined result</div>
            {results.combined_download_path ? (
              <a
                href={agentsApi.downloadUrl(results.combined_download_path)}
                className="agent-download-link"
                style={{ display: "inline-block" }}
              >
                Download combined Excel
              </a>
            ) : results.combined_error ? (
              <p className="case-body" style={{ color: "var(--fail)" }}>
                {results.combined_error}
              </p>
            ) : (
              <p className="case-body" style={{ fontStyle: "italic" }}>
                No file was extracted successfully, so there&rsquo;s nothing to combine.
              </p>
            )}
          </div>

          <div className="ask-bar">
            <button className="ask-button" onClick={reset}>
              Process more files
            </button>
          </div>
        </div>
      )}
    </main>
  );
}
