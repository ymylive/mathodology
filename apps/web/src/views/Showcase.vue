<script setup lang="ts">
// Marketing landing page, ported from reference Showcase.html.
//
// Everything numeric on this page comes from GET /stats/summary?window=7d.
// When the backend has no rows yet we show em-dashes + a muted "pending"
// label — we never fabricate counts, rates, or costs. Single fetch on mount
// is enough; this page isn't a live dashboard.
import { computed, onMounted, ref } from "vue";
import T from "@/components/T.vue";
import { useCountUp } from "@/composables/useCountUp";
import { useInView } from "@/composables/useInView";
import { fetchSummary, type StatsSummary } from "@/api/stats";

const summary = ref<StatsSummary | null>(null);
const loaded = ref(false);

onMounted(async () => {
  try {
    summary.value = await fetchSummary("7d");
  } catch (err) {
    console.error("[Showcase] /stats/summary fetch failed", err);
    summary.value = null;
  } finally {
    loaded.value = true;
  }
});

// Have we actually observed any runs this week?
const hasRuns = computed(
  () => summary.value !== null && summary.value.total_runs > 0,
);

// --- motion: reveal-on-scroll for flow steps and agent cards --------------
const flowSteps = ref<HTMLElement[]>([]);
const agentCards = ref<HTMLElement[]>([]);

function setFlowRef(el: Element | null | { $el: Element }) {
  if (!el || !("nodeType" in el)) return;
  const node = el as HTMLElement;
  if (!flowSteps.value.includes(node)) flowSteps.value.push(node);
}
function setAgentRef(el: Element | null | { $el: Element }) {
  if (!el || !("nodeType" in el)) return;
  const node = el as HTMLElement;
  if (!agentCards.value.includes(node)) agentCards.value.push(node);
}

useInView(flowSteps, (el) => el.classList.add("in"));
useInView(agentCards, (el) => el.classList.add("in"));

// --- motion: count-up targets -------------------------------------------
const statsStrip = ref<HTMLElement | null>(null);
const statsInView = ref(false);
useInView(statsStrip, () => {
  statsInView.value = true;
});

const successTarget = computed(() =>
  hasRuns.value && summary.value ? summary.value.success_rate * 100 : 0,
);
const costTarget = computed(() =>
  hasRuns.value && summary.value && summary.value.median_cost_rmb !== null
    ? summary.value.median_cost_rmb
    : 0,
);
// p95 is milliseconds from the API → seconds for the m:ss display.
const p95TargetSec = computed(() =>
  hasRuns.value && summary.value && summary.value.p95_latency_ms !== null
    ? summary.value.p95_latency_ms / 1000
    : 0,
);

const successDisplay = useCountUp(successTarget, { trigger: statsInView });
const costDisplay = useCountUp(costTarget, { trigger: statsInView });
const p95Display = useCountUp(p95TargetSec, { trigger: statsInView });

function fmtSeconds(n: number): string {
  const m = Math.floor(n / 60);
  const s = Math.floor(n % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

// Cell renderer for the 3-stat strip — handles null / zero-run cases.
interface StatCell {
  render: boolean; // true → show the tweened number, false → show em-dash
}

const successCell = computed<StatCell>(() => ({
  render: hasRuns.value,
}));
const costCell = computed<StatCell>(() => ({
  render:
    hasRuns.value &&
    summary.value !== null &&
    summary.value.median_cost_rmb !== null,
}));
const p95Cell = computed<StatCell>(() => ({
  render:
    hasRuns.value &&
    summary.value !== null &&
    summary.value.p95_latency_ms !== null,
}));
</script>

<template>
  <div class="showcase">
    <main>
      <!-- hero -->
      <section class="hero">
        <div class="wrap">
          <div class="eyebrow">
            <T en="v0.4 · multi-agent framework" zh="v0.4 · 多智能体框架" />
          </div>
          <h1 class="hero-h">
            <T en="Four agents," zh="四个智能体，" /><br />
            <span class="it"><T en="one" zh="协作完成" /></span>
            <span class="mark"><T en="mathematical answer." zh="一份数模答卷。" /></span>
          </h1>
          <p class="hero-sub">
            <T
              en="Mathodology turns a problem statement into a full modelling report — analyse, model, solve, write — with a live Jupyter kernel and any LLM you choose."
              zh="Mathodology 将一段题目描述，自动生成完整的数学建模报告：分析、建模、求解、撰写 —— 配备实时 Jupyter 内核，可自由选择大模型。"
            />
          </p>
          <div class="hero-cta">
            <RouterLink class="btn hi" :to="{ name: 'workbench' }">
              ▶ <T en="Start a run" zh="开始运行" />
            </RouterLink>
            <RouterLink class="btn ghost" :to="{ name: 'dashboard' }">
              <T en="See the dashboard" zh="查看仪表盘" /> →
            </RouterLink>
          </div>
        </div>
      </section>

      <!-- flow -->
      <section class="sec">
        <div class="wrap">
          <div class="eyebrow"><T en="The pipeline" zh="流水线" /></div>
          <h2 class="sec-h"><T en="How a run unfolds." zh="一次运行如何展开。" /></h2>
          <p class="sec-lede">
            <T
              en="Each agent owns one stage. You watch every step — and can intervene at any cell."
              zh="每个智能体负责一个阶段。你可以观察每一步 —— 并在任意单元进行介入。"
            />
          </p>
          <div class="flow">
            <div
              class="flow-step"
              :ref="setFlowRef"
              :style="{ transitionDelay: '0ms' }"
            >
              <div class="n">01</div>
              <div class="t"><T en="Analyze" zh="分析" /></div>
              <div class="d">
                <T
                  en="Parse problem, identify decision variables, objective, constraints."
                  zh="解析题目，识别决策变量、目标函数与约束条件。"
                />
              </div>
            </div>
            <div
              class="flow-step"
              :ref="setFlowRef"
              :style="{ transitionDelay: '70ms' }"
            >
              <div class="n">02</div>
              <div class="t"><T en="Model" zh="建模" /></div>
              <div class="d">
                <T
                  en="Choose a modelling approach — LP, MDP, ODE, MILP — and lay out the math."
                  zh="选择建模范式（LP、MDP、ODE、MILP 等），写出数学表达。"
                />
              </div>
            </div>
            <div
              class="flow-step"
              :ref="setFlowRef"
              :style="{ transitionDelay: '140ms' }"
            >
              <div class="n">03</div>
              <div class="t"><T en="Solve" zh="求解" /></div>
              <div class="d">
                <T
                  en="Write Python, run in a live kernel, iterate until a validated answer."
                  zh="编写 Python，在实时内核中运行，迭代直至得到可验证的结果。"
                />
              </div>
            </div>
            <div
              class="flow-step"
              :ref="setFlowRef"
              :style="{ transitionDelay: '210ms' }"
            >
              <div class="n">04</div>
              <div class="t"><T en="Write" zh="撰写" /></div>
              <div class="d">
                <T
                  en="Assemble a LaTeX report with figures, tables, and citations."
                  zh="整合图表与引用，生成 LaTeX 报告。"
                />
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- agents -->
      <section class="sec">
        <div class="wrap">
          <div class="eyebrow"><T en="Meet the agents" zh="四位智能体" /></div>
          <h2 class="sec-h">
            <T en="Four specialists, one report." zh="四位专才，一份报告。" />
          </h2>
          <div class="ag">
            <!-- Per-agent `claude-haiku · ~4s · ¥0.03` footer lines were
                 fabricated: the gateway never exposes per-agent model
                 preferences or expected durations, so the meta line is
                 omitted rather than invented. -->
            <div class="ag-card" :ref="setAgentRef" :style="{ transitionDelay: '0ms' }">
              <div class="num">A · 01</div>
              <h4><T en="Analyzer" zh="分析员" /></h4>
              <p>
                <T
                  en="Reads the problem. Names every variable, objective, and constraint."
                  zh="通读题目，列出所有变量、目标与约束。"
                />
              </p>
            </div>
            <div class="ag-card" :ref="setAgentRef" :style="{ transitionDelay: '70ms' }">
              <div class="num">B · 02</div>
              <h4><T en="Modeler" zh="建模员" /></h4>
              <p>
                <T
                  en="Picks a model family. Writes math in LaTeX, ready to code."
                  zh="选择模型族，用 LaTeX 写出数学形式，便于编码。"
                />
              </p>
            </div>
            <div class="ag-card" :ref="setAgentRef" :style="{ transitionDelay: '140ms' }">
              <div class="num">C · 03</div>
              <h4><T en="Coder" zh="编程员" /></h4>
              <p>
                <T
                  en="Writes Python cell-by-cell. Runs live, reacts to errors, iterates."
                  zh="逐格编写 Python，实时运行，响应错误，反复迭代。"
                />
              </p>
            </div>
            <div class="ag-card" :ref="setAgentRef" :style="{ transitionDelay: '210ms' }">
              <div class="num">D · 04</div>
              <h4><T en="Writer" zh="撰写员" /></h4>
              <p>
                <T
                  en="Drafts the report. Inlines figures, references methodology, typesets."
                  zh="生成报告正文，嵌入图表，引用方法，排版输出。"
                />
              </p>
            </div>
          </div>
        </div>
      </section>

      <!-- metrics -->
      <section class="sec">
        <div class="wrap">
          <div class="two">
            <div>
              <div class="eyebrow"><T en="In production" zh="已上线" /></div>
              <h2 class="sec-h" v-if="hasRuns && summary">
                {{ summary.total_runs }}
                <T en="runs this week." zh="本周次运行。" />
              </h2>
              <h2 class="sec-h" v-else>
                <T en="Runs this week." zh="本周运行。" />
              </h2>
              <p class="sec-lede">
                <template v-if="hasRuns && summary">
                  <T
                    :en="`Currently tracking ${summary.total_runs} runs across the fleet.`"
                    :zh="`集群中共追踪 ${summary.total_runs} 次运行。`"
                  />
                </template>
                <template v-else-if="loaded">
                  <T
                    en="No runs yet — start one to populate these metrics."
                    zh="暂无运行 —— 开启第一个以生成指标。"
                  />
                </template>
                <template v-else>
                  <T en="Loading metrics…" zh="加载指标中…" />
                </template>
              </p>
            </div>
            <div style="align-self:end;">
              <div class="stat-row" ref="statsStrip">
                <div>
                  <div class="n" v-if="successCell.render">
                    {{ successDisplay.toFixed(1) }}%
                  </div>
                  <div class="n" v-else>—</div>
                  <div class="l">
                    <T en="Success" zh="成功率" />
                    <span v-if="!successCell.render && loaded" class="pending-label">
                      <T en="pending" zh="待定" />
                    </span>
                  </div>
                </div>
                <div>
                  <div class="n" v-if="costCell.render">
                    ¥ {{ costDisplay.toFixed(2) }}
                  </div>
                  <div class="n" v-else>—</div>
                  <div class="l">
                    <T en="Median cost" zh="成本中位" />
                    <span v-if="!costCell.render && loaded" class="pending-label">
                      <T en="pending" zh="待定" />
                    </span>
                  </div>
                </div>
                <div>
                  <div class="n" v-if="p95Cell.render">
                    {{ fmtSeconds(p95Display) }}
                  </div>
                  <div class="n" v-else>—</div>
                  <div class="l">
                    <T en="p95 time" zh="p95 时长" />
                    <span v-if="!p95Cell.render && loaded" class="pending-label">
                      <T en="pending" zh="待定" />
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- cta -->
      <section
        class="sec"
        style="background: var(--ink); color: var(--paper); border-bottom: none;"
      >
        <div class="wrap" style="text-align:center; padding: 40px 0;">
          <h2 class="sec-h" style="color: var(--paper);">
            <T en="Start your first run." zh="开始你的第一次运行。" />
          </h2>
          <p
            class="sec-lede"
            style="color:#BDB4A2; margin-left:auto; margin-right:auto;"
          >
            <T
              en="Paste a problem. Pick a model. Watch four agents work."
              zh="粘贴题目，选择模型，见证四位智能体协作。"
            />
          </p>
          <RouterLink class="btn hi" :to="{ name: 'workbench' }" style="margin-top:8px;">
            ▶ <T en="Open workbench" zh="打开工作台" />
          </RouterLink>
        </div>
      </section>

      <footer class="wrap sc-footer">
        <div>© Mathodology · MIT</div>
        <div>v0.4.1 · <T en="updated today" zh="今日更新" /></div>
      </footer>
    </main>
  </div>
</template>

<style scoped>
.pending-label {
  margin-left: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 9.5px;
  color: var(--ink-3);
  opacity: 0.7;
}
</style>
