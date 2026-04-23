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
