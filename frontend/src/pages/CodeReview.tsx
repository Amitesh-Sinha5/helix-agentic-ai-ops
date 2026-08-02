import { useRef, useState, type FormEvent } from 'react';

import { api, newRequestId } from '../api';
import { AgentTrace } from '../components/AgentTrace';
import { useAgentTrace } from '../hooks/useAgentTrace';
import type { CodeReviewResult, Severity } from '../types';

// Matches CodeReviewRequest.code on the backend; better to say so here than to
// let the server reject a long paste after the round trip.
const MAX_CODE_CHARS = 60_000;

const EXTENSION_LANGUAGES: Record<string, string> = {
  py: 'python',
  js: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  go: 'go',
  rb: 'ruby',
  java: 'java',
  rs: 'rust',
  php: 'php',
  cs: 'csharp',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  sql: 'sql',
  sh: 'bash',
};

function languageFor(filename: string): string {
  const extension = filename.split('.').pop()?.toLowerCase() ?? '';
  return EXTENSION_LANGUAGES[extension] ?? 'python';
}

/**
 * Read a file as text.
 *
 * `Blob.text()` is the clean modern API but is missing in Safari < 14 and in
 * jsdom, so FileReader is the fallback rather than the only path.
 */
function readFileAsText(file: File): Promise<string> {
  if (typeof file.text === 'function') return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error('Could not read the file'));
    reader.readAsText(file);
  });
}

const SAMPLE_CODE = `import subprocess, pickle, hashlib

API_KEY = "sk_live_abcdef1234567890abcdef"

def run_report(user_input, results=[]):
    query = f"SELECT * FROM reports WHERE name = '{user_input}'"
    cursor.execute(query)
    subprocess.run("echo " + user_input, shell=True)
    data = pickle.loads(open("cache.bin", "rb").read())
    digest = hashlib.md5(user_input.encode()).hexdigest()
    try:
        results.append(eval(user_input))
    except:
        print("failed")
    return results`;

const VERDICT_LABEL: Record<string, string> = {
  approve: 'Approve',
  comment: 'Comment',
  request_changes: 'Request changes',
};

const SEVERITY_ORDER: Severity[] = ['critical', 'high', 'medium', 'low'];

export function CodeReview() {
  const [code, setCode] = useState(SAMPLE_CODE);
  const [filename, setFilename] = useState('reports.py');
  const [result, setResult] = useState<CodeReviewResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { events, status, connect, disconnect, merge, reset } = useAgentTrace();
  const codeRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const replaceCode = (next: string, name?: string) => {
    setCode(next.slice(0, MAX_CODE_CHARS));
    if (name) setFilename(name);
    setError(next.length > MAX_CODE_CHARS ? `Truncated to ${MAX_CODE_CHARS} characters.` : null);
    codeRef.current?.focus();
  };

  /** Replace the editor contents with the clipboard, rather than inserting into it. */
  const pasteFromClipboard = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (!text.trim()) {
        setError('Clipboard is empty.');
        return;
      }
      replaceCode(text);
    } catch {
      // Firefox has no readText for pages, and any browser will refuse without
      // permission. Manual paste always works, so say so.
      setError('Could not read the clipboard — click into the box and press ⌘V / Ctrl+V instead.');
      codeRef.current?.focus();
      codeRef.current?.select();
    }
  };

  const onUpload = async (file: File) => {
    try {
      replaceCode(await readFileAsText(file), file.name);
    } catch {
      setError('Could not read that file.');
    } finally {
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!code.trim()) return;

    setRunning(true);
    setError(null);
    setResult(null);
    reset();

    const requestId = newRequestId();
    connect(requestId);
    try {
      const response = await api.analyzeCode(
        { code, language: languageFor(filename), filename },
        requestId,
      );
      setResult(response);
      // Fold in the authoritative trace before the socket is closed below.
      merge(response.trace);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Review failed');
    } finally {
      setRunning(false);
      disconnect();
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Code Review</h1>
          <p className="muted">
            A quality reviewer and a security reviewer run in parallel; a summarizer merges their
            findings into a single structured verdict.
          </p>
        </div>
      </div>

      <section className="card">
        <div className="row wrap">
          <button type="button" className="button button-ghost small" onClick={pasteFromClipboard}>
            Paste from clipboard
          </button>
          <button
            type="button"
            className="button button-ghost small"
            onClick={() => replaceCode('')}
            disabled={!code}
          >
            Clear
          </button>
          <button
            type="button"
            className="button button-ghost small"
            onClick={() => replaceCode(SAMPLE_CODE, 'reports.py')}
          >
            Load sample
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".py,.js,.jsx,.ts,.tsx,.go,.rb,.java,.rs,.php,.cs,.c,.h,.cpp,.sql,.sh,.txt"
            aria-label="Upload a source file"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) void onUpload(file);
            }}
          />
        </div>

        <form onSubmit={onSubmit}>
          <label htmlFor="filename">Filename</label>
          <input id="filename" value={filename} onChange={(e) => setFilename(e.target.value)} />

          <label htmlFor="code">Code</label>
          <textarea
            ref={codeRef}
            id="code"
            className="code-input"
            rows={16}
            value={code}
            spellCheck={false}
            autoCorrect="off"
            autoCapitalize="off"
            placeholder="Paste code here (⌘V / Ctrl+V), or upload a file above."
            onChange={(e) => setCode(e.target.value.slice(0, MAX_CODE_CHARS))}
          />
          <p className="muted small">
            {code.length.toLocaleString()} / {MAX_CODE_CHARS.toLocaleString()} characters ·{' '}
            {code ? code.split('\n').length : 0} lines · detected {languageFor(filename)}
          </p>

          <button type="submit" className="button button-primary" disabled={running || !code.trim()}>
            {running ? 'Reviewing…' : 'Review code'}
          </button>
        </form>

        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </section>

      <AgentTrace events={events} status={status} running={running} />

      {result && (
        <section className="card" data-testid="review-result">
          <div className="answer-header">
            <h2>
              <span className={`badge badge-verdict-${result.verdict}`}>
                {VERDICT_LABEL[result.verdict]}
              </span>
            </h2>
            <div className="answer-badges">
              {SEVERITY_ORDER.filter((s) => result.severity_counts[s]).map((severity) => (
                <span key={severity} className={`badge badge-${severity}`}>
                  {result.severity_counts[severity]} {severity}
                </span>
              ))}
            </div>
          </div>

          <p>{result.summary}</p>
          {result.top_recommendation && (
            <p className="muted">
              <strong>Start here:</strong> {result.top_recommendation}
            </p>
          )}

          {result.issues.length > 0 && (
            <ul className="issue-list">
              {result.issues.map((issue, index) => (
                <li key={`${issue.line}-${issue.title}-${index}`} className="issue" data-testid="issue">
                  <div className="issue-head">
                    <span className={`badge badge-${issue.severity}`}>{issue.severity}</span>
                    <span className="issue-title">{issue.title}</span>
                    {issue.line !== null && <span className="muted small">line {issue.line}</span>}
                    {issue.agent && <span className="badge badge-muted">{issue.agent}</span>}
                  </div>
                  <p className="issue-explanation">{issue.explanation}</p>
                  {issue.suggestion && (
                    <p className="issue-suggestion">
                      <strong>Fix:</strong> {issue.suggestion}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}

          <p className="muted small">
            {result.usage.llm_calls} LLM calls · {result.usage.total_tokens} tokens · $
            {result.usage.cost_usd.toFixed(5)} · {result.usage.latency_ms.toFixed(0)}ms
          </p>
        </section>
      )}
    </div>
  );
}
