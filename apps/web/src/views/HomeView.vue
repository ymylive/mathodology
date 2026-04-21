<script setup lang="ts">
import { computed, ref } from "vue";
import { isFeedVisible, useRunStore } from "@/stores/run";
import EventCard from "@/components/EventCard.vue";
import AgentStreamCard from "@/components/AgentStreamCard.vue";
import AgentOutputCard from "@/components/AgentOutputCard.vue";
import CostMeter from "@/components/CostMeter.vue";
import KernelActivityPanel from "@/components/KernelActivityPanel.vue";
import PaperDraftView from "@/components/PaperDraftView.vue";
import SearchFindingsView from "@/components/SearchFindingsView.vue";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ArrowDown, Play as PlayIcon, RotateCcw, Wifi, WifiOff } from "lucide-vue-next";

const store = useRunStore();
const problemText = ref(
  "Sanity check: print hello-world from the kernel and emit one cost event.",
);

const isBusy = computed(
  () => store.status === "queued" || store.status === "running",
);

// "Reconnecting" badge: WS dropped mid-run but we haven't hit a terminal
// state yet. The ws.ts client reconnects up to 3x with backoff; we just
// mirror its observable state.
const isReconnecting = computed(
  () =>
    !store.wsConnected &&
    (store.status === "running" || store.status === "queued") &&
    store.runId !== null,
);

// Status → shadcn Badge variant + extra tailwind classes for the semantic
// color the default variants don't give us (running=sky, queued=amber, etc).
const statusClass = computed(() => {
  switch (store.status) {
    case "running":
      return "bg-sky-950 text-sky-300 border-sky-900";
    case "queued":
      return "bg-amber-950 text-amber-300 border-amber-900";
    case "done":
      return "bg-emerald-950 text-emerald-300 border-emerald-900";
    case "failed":
      return "bg-red-950 text-red-300 border-red-900";
    default:
      return "bg-secondary text-muted-foreground border-border";
  }
});

// Feed = everything except `token` events (which live in the stream cards).
// Sort by seq so a WS replay after reconnect renders in order.
const feedEvents = computed(() =>
  store.orderedEvents.filter((ev) => isFeedVisible(ev.kind)),
);

// Stream cards: one per agent that has either produced tokens or recorded
// usage. Sorted by a stable agent-order for predictable layout.
const AGENT_ORDER = [
  "analyzer",
  "searcher",
  "modeler",
  "coder",
  "writer",
  "critic",
];

const streamAgents = computed(() => {
  const keys = new Set<string>([
    ...Object.keys(store.tokens),
    ...Object.keys(store.usage),
    ...Object.keys(store.outputs),
  ]);
  const arr = Array.from(keys);
  arr.sort((a, b) => {
    const ia = AGENT_ORDER.indexOf(a);
    const ib = AGENT_ORDER.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
  return arr;
});

// An agent is "active" if the most recent stage.start for it has not yet
// been matched by a stage.done. Cheap O(N) scan over the feed.
const activeAgents = computed(() => {
  const active = new Set<string>();
  for (const ev of store.orderedEvents) {
    if (!ev.agent) continue;
    if (ev.kind === "stage.start") active.add(ev.agent);
    else if (ev.kind === "stage.done") active.delete(ev.agent);
  }
  // Once the run reaches a terminal state, nobody is streaming.
  if (store.status === "done" || store.status === "failed") return new Set<string>();
  return active;
});

async function run() {
  const text = problemText.value.trim();
  if (!text || isBusy.value) return;
  await store.startRun(text);
}

// Writer's PaperDraft is the biggest artifact of a run — we pull it out of
// the agent-column flow and give it a full-width card below. The column
// still shows the stream + a tiny "see paper below" hint so users don't
// miss the transition.
const writerPaper = computed(() => {
  const writer = store.outputs["writer"];
  if (!writer) return null;
  if (writer.schemaName !== "PaperDraft") return null;
  return writer;
});

// Searcher's SearchFindings can easily emit 10+ papers; in the 2-col grid
// that renders as a cramped scrolling list. Promote it to a full-width row
// below (mirroring the Writer treatment) and swap the in-column output for
// a short "see findings below" hint.
const searcherFindings = computed(() => {
  const searcher = store.outputs["searcher"];
  if (!searcher) return null;
  if (searcher.schemaName !== "SearchFindings") return null;
  return searcher;
});
</script>

<template>
  <div class="min-h-full">
    <!-- Sticky top bar: title + status/run pills on the left, ws + cost on
         the right. Always present so the run controls stay reachable while
         the feed scrolls. -->
    <header
      class="sticky top-0 z-20 border-b bg-background/90 backdrop-blur supports-[backdrop-filter]:bg-background/70"
    >
      <div
        class="mx-auto max-w-[1200px] px-4 sm:px-6 py-3 flex items-center gap-3 flex-wrap"
      >
        <h1 class="text-base font-semibold text-foreground tracking-wide">
          Mathodology
        </h1>
        <Separator orientation="vertical" class="h-5 mx-1" />
        <Badge
          variant="outline"
          :class="['mono text-[11px] font-normal', statusClass]"
        >
          {{ store.status }}
        </Badge>
        <Badge
          v-if="isReconnecting"
          variant="outline"
          class="mono text-[11px] font-normal gap-1 border-amber-900 bg-amber-950 text-amber-300"
        >
          <span class="inline-block w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
          reconnecting…
        </Badge>
        <Tooltip v-if="store.runId">
          <TooltipTrigger as-child>
            <span class="mono text-xs text-muted-foreground truncate cursor-default">
              run {{ store.runId.slice(0, 8) }}…
            </span>
          </TooltipTrigger>
          <TooltipContent>
            <span class="mono">{{ store.runId }}</span>
          </TooltipContent>
        </Tooltip>

        <div class="ml-auto flex items-center gap-3">
          <Tooltip>
            <TooltipTrigger as-child>
              <span
                class="inline-flex items-center gap-1 mono text-xs"
                :class="store.wsConnected ? 'text-emerald-400' : 'text-muted-foreground'"
                :aria-label="store.wsConnected ? 'WebSocket connected' : 'WebSocket disconnected'"
              >
                <component
                  :is="store.wsConnected ? Wifi : WifiOff"
                  class="h-3.5 w-3.5"
                  aria-hidden="true"
                />
                <span>ws</span>
              </span>
            </TooltipTrigger>
            <TooltipContent>
              {{ store.wsConnected ? "Live event stream connected" : "Not connected" }}
            </TooltipContent>
          </Tooltip>
          <CostMeter :total-rmb="store.costRmb" />
        </div>
      </div>
    </header>

    <div class="mx-auto max-w-[1200px] px-4 sm:px-6 py-6 space-y-4">
      <!-- Problem input section — always visible, not sticky. The top bar
           handles the run/status affordance for the scrolling case. -->
      <Card class="p-4 space-y-3">
        <label for="problem" class="text-sm text-muted-foreground">
          Problem input
        </label>
        <Textarea
          id="problem"
          v-model="problemText"
          rows="4"
          class="mono text-sm"
          :disabled="isBusy"
          placeholder="Paste a math/modelling problem here..."
        />
        <div class="flex items-center gap-2 flex-wrap">
          <Button
            :disabled="isBusy || !problemText.trim()"
            @click="run"
          >
            <PlayIcon aria-hidden="true" />
            {{ isBusy ? "Running..." : "Run" }}
          </Button>
          <Button
            variant="secondary"
            @click="store.reset()"
          >
            <RotateCcw aria-hidden="true" />
            Reset
          </Button>
        </div>

        <p v-if="store.error" class="text-sm text-red-400 mono">
          {{ store.error }}
        </p>
      </Card>

      <!-- Two-column grid: feed on the left, live streams on the right.
           Collapses to a single column below 768px. -->
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <!-- LEFT: filtered event feed -->
        <Card class="overflow-hidden bg-card/60">
          <header
            class="px-3 py-2 border-b flex items-center justify-between"
          >
            <span class="text-sm text-foreground">Event feed</span>
            <span class="mono text-xs text-muted-foreground tabular-nums">
              {{ feedEvents.length }} event{{ feedEvents.length === 1 ? "" : "s" }}
            </span>
          </header>
          <div v-if="feedEvents.length === 0" class="px-4 py-8 text-center">
            <p class="text-sm text-muted-foreground">
              No events yet. Click <span class="mono">Run</span> to start a stream.
            </p>
          </div>
          <div v-else class="max-h-[60vh] overflow-y-auto">
            <EventCard
              v-for="ev in feedEvents"
              :key="`${ev.run_id}-${ev.seq}`"
              :event="ev"
            />
          </div>
        </Card>

        <!-- RIGHT: live per-agent streams -->
        <section class="space-y-3">
          <Card
            v-if="streamAgents.length === 0"
            class="bg-card/60 px-4 py-8 text-center"
          >
            <p class="text-sm text-muted-foreground">
              Live agent streams will appear here once a run starts.
            </p>
          </Card>
          <div
            v-for="agent in streamAgents"
            :key="agent"
            class="space-y-2"
          >
            <AgentStreamCard
              :agent="agent"
              :text="store.tokens[agent]?.text ?? ''"
              :model="store.tokens[agent]?.model ?? null"
              :usage="store.usage[agent] ?? null"
              :active="activeAgents.has(agent)"
            />
            <!-- Coder: live kernel activity between stream + structured output.
                 The panel hides itself when there are no cells. -->
            <KernelActivityPanel v-if="agent === 'coder'" />
            <!-- Writer + PaperDraft: suppress the narrow AgentOutputCard
                 and drop a hint pointing to the full-width card below. -->
            <div
              v-if="agent === 'writer' && writerPaper"
              class="rounded-md border border-sky-900/60 bg-sky-950/20 px-3 py-2 text-xs text-sky-300 mono inline-flex items-center gap-1.5"
            >
              <ArrowDown class="h-3.5 w-3.5" aria-hidden="true" />
              <span>Paper rendered below</span>
            </div>
            <!-- Searcher + SearchFindings: same full-width treatment as
                 Writer — a hit list with ~10 papers reads much better wide. -->
            <div
              v-else-if="agent === 'searcher' && searcherFindings"
              class="rounded-md border border-sky-900/60 bg-sky-950/20 px-3 py-2 text-xs text-sky-300 mono inline-flex items-center gap-1.5"
            >
              <ArrowDown class="h-3.5 w-3.5" aria-hidden="true" />
              <span>Search findings rendered below</span>
            </div>
            <AgentOutputCard
              v-else-if="store.outputs[agent]"
              :agent="agent"
              :schema-name="store.outputs[agent].schemaName"
              :output="store.outputs[agent].output"
              :duration-ms="store.outputs[agent].durationMs"
            />
          </div>
        </section>
      </div>

      <!-- Full-width search findings card: rendered above the paper (and
           below the 2-col grid) because Searcher runs before Writer in the
           pipeline, so keeping the visual order matches the event timeline. -->
      <Card
        v-if="searcherFindings"
        class="overflow-hidden border-sky-900/60 bg-sky-950/10"
        aria-label="Search findings"
      >
        <header
          class="px-4 py-2 border-b border-sky-900/60 flex items-center gap-2"
        >
          <span
            class="inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style="background-color: var(--color-agent-searcher)"
            aria-hidden="true"
          />
          <span class="text-sm text-foreground">Search findings</span>
          <Badge
            variant="outline"
            class="mono text-[11px] py-0 px-1.5 font-normal border-sky-900 bg-sky-950/60 text-sky-300"
          >
            SearchFindings
          </Badge>
          <Badge
            v-if="searcherFindings.durationMs !== null"
            variant="outline"
            class="mono text-[11px] py-0 px-1.5 font-normal text-muted-foreground tabular-nums"
          >
            {{
              searcherFindings.durationMs < 1000
                ? `${searcherFindings.durationMs} ms`
                : `${(searcherFindings.durationMs / 1000).toFixed(1)} s`
            }}
          </Badge>
        </header>
        <div class="px-4 py-4">
          <SearchFindingsView
            :output="searcherFindings.output"
            :run-id="store.runId ?? ''"
          />
        </div>
      </Card>

      <!-- Full-width paper card: Writer's PaperDraft is the main deliverable
           so it breaks out of the 2-column flow and spans the full page. -->
      <Card
        v-if="writerPaper && store.runId"
        class="overflow-hidden border-sky-900/60 bg-sky-950/10"
        aria-label="Paper draft"
      >
        <header
          class="px-4 py-2 border-b border-sky-900/60 flex items-center gap-2"
        >
          <span
            class="inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style="background-color: var(--color-agent-writer)"
            aria-hidden="true"
          />
          <span class="text-sm text-foreground">Paper draft</span>
          <Badge
            variant="outline"
            class="mono text-[11px] py-0 px-1.5 font-normal border-sky-900 bg-sky-950/60 text-sky-300"
          >
            PaperDraft
          </Badge>
          <Badge
            v-if="writerPaper.durationMs !== null"
            variant="outline"
            class="mono text-[11px] py-0 px-1.5 font-normal text-muted-foreground tabular-nums"
          >
            {{
              writerPaper.durationMs < 1000
                ? `${writerPaper.durationMs} ms`
                : `${(writerPaper.durationMs / 1000).toFixed(1)} s`
            }}
          </Badge>
        </header>
        <div class="px-4 py-4">
          <PaperDraftView :output="writerPaper.output" :run-id="store.runId" />
        </div>
      </Card>
    </div>
  </div>
</template>
