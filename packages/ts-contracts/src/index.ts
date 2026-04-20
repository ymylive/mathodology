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
