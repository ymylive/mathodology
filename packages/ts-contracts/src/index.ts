// Re-exports the OpenAPI-generated types. Run `just gen-ts` to regenerate.
export * from "./generated";

// Hand-written event envelope matches packages/contracts/events.schema.json.
export type EventKind =
  | "stage.start"
  | "stage.done"
  | "log"
  | "token"
  | "cost"
  | "agent.output"
  | "kernel.stdout"
  | "kernel.figure"
  | "error"
  | "done";

export type AgentName =
  | "analyzer"
  | "modeler"
  | "coder"
  | "writer"
  | "critic"
  | "searcher"
  | null;

export interface AgentEvent {
  run_id: string;
  agent: AgentName;
  kind: EventKind;
  seq: number;
  ts: string;
  payload: Record<string, unknown>;
}

export type CritiqueSeverity = "info" | "minor" | "major" | "blocking";
export type CriticRole =
  | "modeling_coach"
  | "academic_reviewer"
  | "code_reviewer";
export type ReviewTargetAgent =
  | "analyzer"
  | "searcher"
  | "modeler"
  | "coder"
  | "writer";

export interface CritiqueFinding {
  severity: CritiqueSeverity;
  area: string;
  message: string;
  evidence: string;
  required_change: string;
}

export interface CritiqueChecklistItem {
  id: string;
  label: string;
  passed: boolean;
  evidence: string;
}

export interface RoleCritique {
  role: CriticRole;
  passed: boolean;
  score: number;
  summary: string;
  findings: CritiqueFinding[];
}

export interface CritiqueReport {
  target_agent: ReviewTargetAgent;
  target_schema: string;
  passed: boolean;
  score: number;
  summary: string;
  findings: CritiqueFinding[];
  required_changes: string[];
  roles?: RoleCritique[];
  checklist?: CritiqueChecklistItem[];
  revision_round?: number;
  max_revision_rounds?: number;
  budget_exhausted?: boolean;
}

// Hand-written figure / paper-meta shapes. Mirrors
// `packages/py-contracts/src/mm_contracts/agent_io.py::Figure` and the
// `paper.meta.json` structure written by the worker's pipeline. Consumed by
// the gateway's PDF/DOCX/LaTeX export path.
export interface Figure {
  id: string;
  caption: string;
  path_png: string;
  path_svg: string | null;
  width: number;
}

export interface PaperSectionMeta {
  title: string;
  body_markdown: string;
}

export interface PaperMeta {
  title: string;
  abstract: string;
  competition_type: "mcm" | "icm" | "cumcm" | "huashu" | "other";
  problem_text: string;
  sections: PaperSectionMeta[];
  references: string[];
  figures: Figure[];
}

// Hand-written search-routing contract. Mirrors
// `packages/py-contracts/src/mm_contracts/agent_io.py::SearchConfig`.
// The frontend lets the user pick `primary` + engine list; the worker
// picks up this config off `ProblemInput.search_config` and falls back
// to env defaults when it's null.
export type SearchPrimary = "tavily" | "open_websearch" | "none";

export type SearchEngine =
  | "bing"
  | "baidu"
  | "duckduckgo"
  | "csdn"
  | "juejin"
  | "brave"
  | "exa"
  | "startpage";

export interface SearchConfig {
  primary: SearchPrimary;
  engines: SearchEngine[];
  tavily_depth: "basic" | "advanced";
  fallback_threshold: number;
}

// Hand-written Searcher agent output contract. Mirrors
// `packages/py-contracts/src/mm_contracts/agent_io.py::Paper` and
// `SearchFindings`. Consumed by downstream agents (Writer, etc.) to
// ground citations and retrieve enriched paper metadata.
export interface Paper {
  title: string;
  authors?: string[];
  abstract?: string;
  url: string;
  arxiv_id?: string | null;
  doi?: string | null;
  published?: string | null;
  relevance_reason?: string | null;
}

export interface SearchFindings {
  queries?: string[];
  papers?: Paper[];
  key_findings?: string[];
  datasets_mentioned?: string[];
  paper_fulltext_paths?: string[];
}
