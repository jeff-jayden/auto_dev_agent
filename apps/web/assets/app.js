const $ = (selector) => document.querySelector(selector);
let currentTask = null;
let currentJob = null;
let watchGeneration = 0;
let knownTasks = [];
let renderedChatTaskId = null;
let renderedChatRevision = "";
let renderedChatVisible = false;
let activeView = "requirement";
let observabilityTaskId = null;
let folderPickerState = null;
let traceTimelineLinks = new Map();
let observabilityTimelineEvents = [];
let renderedTimelineTaskId = null;
let renderedTimelineLatestEvent = null;
let currentRecovery = null;

const chatBottomThreshold = 48;

const terminalJobStatuses = new Set(["succeeded", "failed", "cancelled", "paused", "abandoned"]);
const jobStatusLabels = {
  queued: "排队中",
  running: "执行中",
  pause_requested: "等待安全暂停点",
  paused: "已暂停",
  cancelled: "已取消",
  succeeded: "已完成",
  failed: "执行失败",
  abandoned: "旧执行已终止",
};

const statusLabels = {
  requirement_analysis: "需求分析中",
  waiting_requirement_input: "等待补充信息",
  waiting_requirement_approval: "等待方案审批",
  plan_approved: "方案已批准",
  waiting_risk_approval: "等待风险审批",
  developing: "开发中", testing: "测试中", change_ready: "变更已就绪",
  repairing: "自动修复中",
  generating_mr: "生成 MR 中", reviewing: "Code Review 中",
  changes_requested: "CR 要求修改", review_repairing: "CR 自动修复中",
  review_approved: "CR 已通过", waiting_release_approval: "等待发布审批",
  publishing_pull_request: "正在发布 PR", waiting_merge_approval: "等待合入审批",
  merging: "正在合入", merged: "已合入",
  rejected: "方案已拒绝", failed: "执行失败",
};

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[character]);
}

function parseUnifiedDiff(diff) {
  const files = [];
  let file = null;
  let oldLine = 0;
  let newLine = 0;
  for (const raw of String(diff || "").split("\n")) {
    if (raw.startsWith("diff --git ")) {
      const match = raw.match(/^diff --git a\/(.+) b\/(.+)$/);
      file = {path: match ? match[2] : raw.slice(11), rows: [], additions: 0, deletions: 0};
      files.push(file);
      continue;
    }
    if (!file || raw.startsWith("index ") || raw.startsWith("--- ") || raw.startsWith("+++ ")) continue;
    if (raw.startsWith("@@")) {
      const match = raw.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/);
      if (match) { oldLine = Number(match[1]); newLine = Number(match[2]); }
      file.rows.push({type: "hunk", text: raw});
      continue;
    }
    if (raw.startsWith("\\ No newline")) {
      file.rows.push({type: "meta", text: raw});
    } else if (raw.startsWith("+")) {
      file.rows.push({type: "add", oldLine: "", newLine: newLine++, marker: "+", text: raw.slice(1)});
      file.additions += 1;
    } else if (raw.startsWith("-")) {
      file.rows.push({type: "delete", oldLine: oldLine++, newLine: "", marker: "−", text: raw.slice(1)});
      file.deletions += 1;
    } else {
      file.rows.push({type: "context", oldLine: oldLine++, newLine: newLine++, marker: " ", text: raw.startsWith(" ") ? raw.slice(1) : raw});
    }
  }
  return files;
}

function renderDiff(diff, targetSelector = "#diff") {
  const files = parseUnifiedDiff(diff);
  if (!files.length) {
    $(targetSelector).innerHTML = '<p class="diff-empty">当前没有代码差异。</p>';
    return;
  }
  $(targetSelector).innerHTML = files.map((file) => {
    const rows = file.rows.map((row) => {
      if (row.type === "hunk" || row.type === "meta") {
        return `<tr class="diff-${row.type}"><td colspan="4"><code>${escapeHtml(row.text)}</code></td></tr>`;
      }
      return `<tr class="diff-${row.type}"><td class="diff-line-number">${row.oldLine}</td><td class="diff-line-number">${row.newLine}</td><td class="diff-marker">${escapeHtml(row.marker)}</td><td class="diff-code"><code>${escapeHtml(row.text)}</code></td></tr>`;
    }).join("");
    return `<details class="diff-file" open><summary><span class="diff-file-name">▾ ${escapeHtml(file.path)}</span><span class="diff-stat"><b class="diff-add-count">+${file.additions}</b><b class="diff-delete-count">−${file.deletions}</b></span></summary><div class="diff-table-wrap"><table><tbody>${rows}</tbody></table></div></details>`;
  }).join("");
}

function analyzeAttemptFailure(attempt) {
  const output = String(attempt.output || "");
  const normalized = output.toLowerCase();
  if (normalized.includes("proposal removes existing architectural wiring")) {
    return "修改方案在写入前被安全校验拦截：系统认为它可能删除已有导入、组件入口或调用关系。本轮没有写入代码，也没有运行测试。";
  }
  if (normalized.includes("outside the approved technical plan") || normalized.includes("outside allowed")) {
    return "模型尝试修改技术方案未批准的文件，系统已在写入前阻止。本轮没有产生代码变更。";
  }
  if (normalized.includes("corrupt patch") || normalized.includes("patch application failed")) {
    return "模型生成的代码补丁格式不完整，系统无法安全应用，因此没有执行后续测试。";
  }
  if (normalized.includes("does not match") || normalized.includes("not found in")) {
    return "模型指定的原代码片段与当前文件不一致，修改无法准确定位，因此没有写入文件。";
  }
  if (normalized.includes("does not change any content") || normalized.includes("no-op")) {
    return "模型给出的替换内容与当前代码相同，没有产生有效修改，需要重新定位真正需要调整的位置。";
  }
  if (normalized.includes("timed out") || normalized.includes("timeout")) {
    return "执行超过时间限制被终止。代码可能已经修改，请先查看相关文件，再决定是否重试。";
  }
  if (attempt.exit_code === -1) {
    return "修改在代码写入或测试开始前失败。本轮通常没有产生有效 Diff，可以展开执行日志查看技术细节。";
  }
  return `代码修改后验证未通过，测试命令退出码为 ${attempt.exit_code}。请查看相关文件确认改动，再展开执行日志定位具体测试错误。`;
}

function attemptRelatedFiles(attempt) {
  const proposal = attempt.proposal || {};
  return [...new Set([
    ...(attempt.changed_files || []),
    ...(proposal.replacements || []).map((item) => item.path),
    ...(proposal.changes || []).map((item) => item.path),
  ].filter(Boolean))];
}

function renderAttemptFiles(attempt, index) {
  const files = attemptRelatedFiles(attempt);
  if (!files.length) return "";
  const proposal = attempt.proposal || {};
  const proposedChanges = [
    ...(proposal.replacements || []).map((item) => ({...item, kind: "replacement"})),
    ...(proposal.changes || []).map((item) => ({...item, kind: "patch"})),
  ];
  let content;
  if (attempt.diff) {
    content = `<div id="attempt-diff-${index}" class="attempt-related-diff"></div>`;
  } else if (proposedChanges.length) {
    content = proposedChanges.map((change) => change.kind === "replacement"
      ? `<article class="attempt-file-change"><header><code>${escapeHtml(change.path)}</code><span>拟修改 · 尚未写入</span></header><div class="attempt-change-grid"><div><b>修改前</b><pre>${escapeHtml(change.search)}</pre></div><div><b>拟修改为</b><pre>${escapeHtml(change.replace)}</pre></div></div></article>`
      : `<article class="attempt-file-change"><header><code>${escapeHtml(change.path)}</code><span>拟修改 · 尚未写入</span></header><pre>${escapeHtml(change.patch)}</pre></article>`
    ).join("");
  } else {
    content = `<div class="attempt-file-list">${files.map((file) => `<code>${escapeHtml(file)}</code>`).join("")}</div>`;
  }
  return `<details class="attempt-files"><summary>查看相关文件 <span>${files.length}</span></summary>${content}</details>`;
}

function renderAttemptToolCalls(attempt) {
  const calls = attempt.tool_calls || [];
  if (!calls.length) return "";
  const items = calls.map((call, index) => {
    const args = Object.entries(call.arguments || {}).map(([key, value]) => {
      const rendered = typeof value === "object" ? JSON.stringify(value) : value;
      return `<code><b>${escapeHtml(key)}</b>=${escapeHtml(rendered)}</code>`;
    }).join("");
    return `<article class="attempt-tool-call ${call.success === false ? "failed" : ""}"><header><b>${String(index + 1).padStart(2, "0")} · ${escapeHtml(call.tool)}</b><span>${call.success === false ? "失败" : "成功"}</span></header><p>${escapeHtml(call.summary || "已执行")}</p>${args ? `<div>${args}</div>` : ""}</article>`;
  }).join("");
  return `<details class="attempt-tools"><summary>Agent 工具调用 <span>${calls.length}</span></summary><div class="attempt-tool-list">${items}</div></details>`;
}

function cumulativeDiff(task) {
  return task?.metadata?.workspace_diff || task?.result?.diff || "";
}

const viewLabels = {
  requirement: ["REQUIREMENT", "需求工作台"],
  development: ["DEVELOPMENT", "需求开发"],
  review: ["CODE REVIEW", "代码 CR"],
  observability: ["OBSERVABILITY", "Agent 可观测性"],
  attempts: ["DEVELOPMENT LOG", "开发尝试"],
  create: ["NEW TASK", "新建任务"],
};

function defaultViewForTask(task) {
  return task?.result ? "development" : "requirement";
}

function updateNavigationAvailability() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.disabled = button.dataset.view !== "requirement" && !currentTask;
  });
}

function setView(view) {
  if (view !== "create" && view !== "requirement" && !currentTask) return;
  activeView = view;
  const label = viewLabels[view] || viewLabels.requirement;
  $("#view-eyebrow").textContent = label[0];
  $("#view-title").textContent = label[1];
  $("#create-view").classList.toggle("hidden", view !== "create");
  $("#empty-state").classList.toggle("hidden", Boolean(currentTask) || view === "create");
  $("#task-view").classList.toggle("hidden", !currentTask || view === "create");
  ["requirement", "development", "review", "observability", "attempts"].forEach((name) => {
    const element = $(`#${name}-view`);
    if (element) element.classList.toggle("hidden", !currentTask || view !== name);
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", view !== "create" && button.dataset.view === view);
  });
  updateNavigationAvailability();
  if (view === "observability" && currentTask && observabilityTaskId !== currentTask.id) {
    observabilityTaskId = currentTask.id;
    renderObservability(currentTask.id).catch((error) => {
      observabilityTaskId = null;
      notify(error.message);
    });
  }
}
function list(element, items = []) { element.innerHTML = items.map((item) => `<li>${escapeHtml(item)}</li>`).join(""); }
function notify(message) {
  const toast = $("#toast"); toast.textContent = message; toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), 2800);
}
function isChatNearBottom(element) {
  return !element || element.scrollHeight - element.scrollTop - element.clientHeight <= chatBottomThreshold;
}
function scrollChatToLatest(element) {
  requestAnimationFrame(() => { element.scrollTop = element.scrollHeight; });
}
async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const contentType = response.headers.get("content-type") || "";
  let data;
  if (contentType.includes("application/json")) {
    data = await response.json();
  } else {
    const text = await response.text();
    data = {detail: text || `接口返回 HTTP ${response.status}`};
  }
  if (!response.ok) throw new Error(data.detail || `请求失败（HTTP ${response.status}）`);
  return data;
}
async function loadRepositories(selectedId = null) {
  const repositories = await api("/api/repositories");
  const select = $("#repository");
  select.innerHTML = repositories.map((repo) => `<option value="${escapeHtml(repo.id)}">${escapeHtml(repo.name)} · ${repo.provider === "github" ? "GitHub" : "本地 Git"}</option>`).join("");
  if (selectedId) select.value = selectedId;
}

function renderTaskList() {
  const markup = knownTasks.map((task) => {
    const active = currentTask?.id === task.id ? " active" : "";
    const recoverable = task.recovery?.status === "recoverable";
    const label = statusLabels[task.status] || task.status;
    const relation = task.metadata?.followup_of ? "后续任务 · " : "";
    return `<button type="button" class="task-list-item${active}${recoverable ? " has-recovery" : ""}" data-task-id="${escapeHtml(task.id)}"><b>${escapeHtml(task.title)}</b>${recoverable ? '<em class="task-recovery-badge">可恢复</em>' : ""}<span>${escapeHtml(task.requirement || "暂无需求描述")}</span><small>${escapeHtml(relation + label)} · ${new Date(task.updated_at).toLocaleString()}</small></button>`;
  }).join("") || '<p class="task-list-empty">还没有任务，点击＋创建第一个任务。</p>';
  ["#task-list", "#task-list-active"].forEach((selector) => {
    const listElement = $(selector);
    if (listElement) listElement.innerHTML = markup;
  });
  document.querySelectorAll(".task-list-item").forEach((item) => item.addEventListener("click", async () => {
    try { await selectTask(item.dataset.taskId, "requirement"); }
    catch (error) { notify(error.message); }
  }));
}

const recoveryStageLabels = {
  plan_approved: "方案已批准",
  proposal_ready: "修改方案已生成",
  patch_applied: "代码修改已应用",
  test_result_saved: "测试结果已保存",
  review_round_saved: "审查结果已保存",
};
const recoveryActionLabels = {
  prepare_workspace: "创建隔离工作区",
  apply_patch: "应用代码修改",
  run_tests: "执行测试",
  generate_mr: "生成 MR",
  run_review: "执行 Code Review",
  repair: "继续自动修复",
  stop_failed: "处理失败结果",
};

function renderRecovery(report) {
  currentRecovery = report;
  const panel = $("#recovery-panel");
  const visible = report && ["recoverable", "blocked"].includes(report.status);
  panel.classList.toggle("hidden", !visible);
  if (!visible) return;
  const stage = recoveryStageLabels[report.checkpoint_stage] || report.checkpoint_stage || "尚无安全检查点";
  $("#recovery-summary").textContent = report.reason;
  const runtime = report.current_stage
    ? `当前停在：${statusLabels[report.current_stage] || report.current_stage}${report.heartbeat_at ? ` · 最后心跳 ${new Date(report.heartbeat_at).toLocaleString()}` : ""}`
    : "";
  $("#recovery-checkpoint-summary").textContent = report.next_action
    ? `最后完成：${stage} · 下一步：${recoveryActionLabels[report.next_action] || report.next_action}`
    : `诊断结果：${stage}`;
  if (runtime) $("#recovery-checkpoint-summary").textContent += ` · ${runtime}`;
  const resumeButton = $("#recover-task-button");
  resumeButton.classList.toggle("hidden", report.status !== "recoverable");
  resumeButton.disabled = report.status !== "recoverable";
  resumeButton.innerHTML = report.active_job_stale
    ? "终止旧执行并恢复 <b>→</b>"
    : "从检查点继续 <b>→</b>";
}

function openRecoveryDetails() {
  if (!currentRecovery) return;
  const report = currentRecovery;
  const recoverable = report.status === "recoverable";
  $("#recovery-detail-status").textContent = recoverable ? "可恢复" : "需要人工处理";
  $("#recovery-detail-status").className = `status ${recoverable ? "recoverable" : "failed"}`;
  $("#recovery-detail-reason").textContent = report.reason;
  const stage = recoveryStageLabels[report.checkpoint_stage] || report.checkpoint_stage || "无";
  $("#recovery-detail-next").textContent = report.next_action
    ? `恢复点：${stage}；继续后执行：${recoveryActionLabels[report.next_action] || report.next_action}`
    : "当前不能自动恢复，请先处理诊断中的阻塞项。";
  if (report.current_stage) {
    $("#recovery-detail-next").textContent += `；中断阶段：${statusLabels[report.current_stage] || report.current_stage}`;
  }
  $("#recovery-check-list").innerHTML = (report.checks || []).map((check) => {
    const icon = check.status === "blocked" ? "×" : (check.status === "running" ? "…" : "✓");
    return `<div class="recovery-check ${escapeHtml(check.status)}"><span>${icon}</span><div><b>${escapeHtml(check.name)}</b><small>${escapeHtml(check.detail)}</small></div></div>`;
  }).join("");
  $("#recovery-drawer-resume").classList.toggle("hidden", !recoverable);
  $("#recovery-drawer-resume").disabled = !recoverable;
  $("#recovery-drawer-resume").innerHTML = report.active_job_stale
    ? "终止旧执行并恢复 <b>→</b>"
    : "从检查点继续 <b>→</b>";
  $("#recovery-drawer").classList.remove("hidden");
}

function closeRecoveryDetails() {
  $("#recovery-drawer").classList.add("hidden");
}

function renderReviewGate(task) {
  const repairing = task.status === "review_repairing";
  const visible = ["changes_requested", "review_repairing"].includes(task.status);
  $("#review-gate").classList.toggle("hidden", !visible);
  $("#review-gate-message").textContent = repairing
    ? "Agent 正在后台修复阻塞问题并重新执行 Review，请等待当前执行结束。"
    : "建议先处理 findings，必要时可人工批准。";
  const button = $("#rerun-review-button");
  button.disabled = repairing;
  button.textContent = repairing ? "Review 正在执行…" : "重新审查";
  $("#approve-review-button").disabled = repairing;
}

function renderUIAcceptance(task) {
  const panel = $("#ui-acceptance-panel");
  const design = task.design_reference;
  panel.classList.toggle("hidden", !design);
  if (!design) return;
  const report = task.ui_acceptance;
  $("#ui-acceptance-summary").textContent = report?.summary || "Figma 设计基线已保存，尚未执行浏览器验收";
  const similarity = report?.similarity_score == null ? "未计算" : `${report.similarity_score.toFixed(1)}%`;
  $("#ui-acceptance-metrics").innerHTML = [
    ["Figma 节点", design.node_id],
    ["验收视口", `${design.viewport_width}×${design.viewport_height}`],
    ["视觉相似度", similarity],
    ["验收状态", report?.status || "未执行"],
  ].map(([label, value]) => `<span><small>${escapeHtml(label)}</small> <b>${escapeHtml(value)}</b></span>`).join("");
  $("#ui-acceptance-checks").innerHTML = (report?.checks || []).map((check) =>
    `<article class="ui-check ${escapeHtml(check.status)}"><b>${escapeHtml(check.name)} · ${escapeHtml(check.status)}</b><small>${escapeHtml(check.detail)}${check.blocking ? " · 阻塞发布" : ""}</small></article>`
  ).join("") || '<p class="task-list-empty">开发完成后自动执行；也可以点击“重新验收”。</p>';
  const images = [];
  if (design.screenshot_path) images.push(["Figma 参考图", "figma"]);
  if (report?.implementation_screenshot_path) images.push(["实现页面", "implementation"]);
  if (report?.diff_screenshot_path) images.push(["视觉差异图", "diff"]);
  $("#ui-acceptance-images").innerHTML = images.map(([label, kind]) => {
    const url = `/api/tasks/${encodeURIComponent(task.id)}/ui-acceptance/image/${kind}?v=${encodeURIComponent(report?.created_at || design.captured_at)}`;
    return `<a href="${url}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}<img src="${url}" alt="${escapeHtml(label)}" loading="lazy" /></a>`;
  }).join("");
  $("#rerun-ui-acceptance-button").disabled = !design.preview_url;
}

async function refreshTaskList() {
  knownTasks = await api("/api/tasks");
  renderTaskList();
  return knownTasks;
}

function showNewTask() {
  watchGeneration += 1;
  renderedChatTaskId = null;
  renderedChatRevision = "";
  renderedChatVisible = false;
  $("#task-form").reset();
  renderTaskList();
  setView("create");
}

async function selectTask(taskId, requestedView = null) {
  watchGeneration += 1;
  currentJob = null;
  $("#job-panel").classList.add("hidden");
  $("#replay-result").classList.add("hidden");
  const task = await api(`/api/tasks/${taskId}`);
  await render(task);
  setView(requestedView || defaultViewForTask(task));
  renderTaskList();
  const jobs = await api(`/api/tasks/${task.id}/jobs`);
  if (!jobs.length) return;
  renderJob(jobs[0]);
  if (["queued", "running", "pause_requested"].includes(jobs[0].status)) {
    watchJob(jobs[0]).catch((error) => notify(error.message));
  }
}

function correlateSpansToTimeline(spans, traceId) {
  const traceSteps = [...traceTimelineLinks.entries()].filter(([, linkedTraceId]) => linkedTraceId === traceId).map(([index]) => index + 1);
  const semanticTypes = {
    "repository.analyze": ["repository_analyzed"],
    "agent.plan": ["plan_ready"],
    "llm.PlanningResponse": ["plan_ready"],
    "llm.ReplacementDevelopmentProposal": ["checkpoint_saved"],
    "tool.apply_proposal": ["checkpoint_saved"],
    "tool.run_tests": ["checkpoint_saved", "development_attempt"],
    "tool.git_diff": ["merge_request_generated", "merge_request_regenerated"],
    "llm.ReviewerModelOutput": ["code_review_completed"],
  };
  const messageMarkers = {
    "llm.ReplacementDevelopmentProposal": ["proposal_ready"],
    "tool.apply_proposal": ["patch_applied"],
    "tool.run_tests": ["test_result_saved", "测试退出码"],
  };
  const result = new Map();
  spans.forEach((span) => {
    if (span.kind === "agent" && span.name !== "agent.plan") {
      result.set(span.id, traceSteps);
      return;
    }
    const allowedTypes = semanticTypes[span.name];
    if (!allowedTypes) return;
    const markers = messageMarkers[span.name] || [];
    const start = new Date(span.started_at).getTime() - 1000;
    const end = (span.ended_at ? new Date(span.ended_at).getTime() : new Date(span.started_at).getTime()) + 2500;
    const candidates = traceSteps.filter((step) => {
      const event = observabilityTimelineEvents[step - 1];
      if (!event || !allowedTypes.includes(event.event_type)) return false;
      if (markers.length && !markers.some((marker) => String(event.message || "").includes(marker))) return false;
      const eventTime = new Date(event.created_at).getTime();
      return eventTime >= start && eventTime <= end;
    });
    if (!candidates.length) return;
    const endedAt = span.ended_at ? new Date(span.ended_at).getTime() : new Date(span.started_at).getTime();
    const nearestDistance = Math.min(...candidates.map((step) => Math.abs(new Date(observabilityTimelineEvents[step - 1].created_at).getTime() - endedAt)));
    result.set(span.id, candidates.filter((step) => Math.abs(new Date(observabilityTimelineEvents[step - 1].created_at).getTime() - endedAt) <= nearestDistance + 800));
  });
  return result;
}

function centerTimelineItem(item) {
  const timeline = $("#timeline");
  if (!timeline || !item) return;
  const maxScroll = Math.max(0, timeline.scrollWidth - timeline.clientWidth);
  if (maxScroll <= 1) return;
  const desired = item.offsetLeft - (timeline.clientWidth - item.offsetWidth) / 2;
  const target = Math.max(0, Math.min(maxScroll, desired));
  if (Math.abs(timeline.scrollLeft - target) > 2) {
    timeline.scrollTo({left: target, behavior: "smooth"});
  }
}

function focusSpanTimeline(steps, spanId = null) {
  document.querySelectorAll(".timeline-item").forEach((item) => item.classList.remove("trace-active", "span-active"));
  document.querySelectorAll(".span-item").forEach((item) => item.classList.toggle("timeline-active", item.dataset.spanId === spanId));
  const linkedItems = steps.map((step) => document.querySelector(`.timeline-item[data-step="${step}"]`)).filter(Boolean);
  linkedItems.forEach((item) => item.classList.add("span-active"));
  if (linkedItems.length) centerTimelineItem(linkedItems[Math.floor((linkedItems.length - 1) / 2)]);
}

async function showTrace(traceId, focusEventStep = null) {
  const detail = await api(`/api/traces/${traceId}`);
  document.querySelectorAll(".trace-item").forEach((item) => item.classList.toggle("active", item.dataset.traceId === traceId));
  const kindLabels = {agent: "AGENT", llm: "LLM", tool: "TOOL", workflow: "FLOW", execution: "EXEC"};
  const indexed = (detail.spans || []).map((span, originalIndex) => ({span, originalIndex}));
  indexed.sort((left, right) => new Date(left.span.started_at) - new Date(right.span.started_at) || left.originalIndex - right.originalIndex);
  const spansById = new Map(indexed.map(({span}) => [span.id, span]));
  const spanTimelineLinks = correlateSpansToTimeline(indexed.map(({span}) => span), traceId);
  const depthOf = (span) => {
    let depth = 0; let parentId = span.parent_span_id; const visited = new Set();
    while (parentId && spansById.has(parentId) && !visited.has(parentId) && depth < 4) {
      visited.add(parentId); depth += 1; parentId = spansById.get(parentId).parent_span_id;
    }
    return depth;
  };
  $("#span-list").innerHTML = indexed.map(({span}, index) => {
    const depth = depthOf(span);
    const attrs = Object.entries(span.attributes || {}).map(([key, value]) => `<span class="span-attribute"><b>${escapeHtml(key)}</b>${escapeHtml(typeof value === "object" ? JSON.stringify(value) : value)}</span>`).join("");
    const detailText = span.error || span.output_summary || span.input_summary || "";
    const startedAt = new Date(span.started_at);
    const time = Number.isNaN(startedAt.getTime()) ? "" : startedAt.toLocaleTimeString([], {hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit"});
    const timelineSteps = spanTimelineLinks.get(span.id) || [];
    const timelineLabel = timelineSteps.length ? (timelineSteps.length === 1 ? `时间线 ${String(timelineSteps[0]).padStart(2, "0")}` : `时间线 ${String(timelineSteps[0]).padStart(2, "0")}–${String(timelineSteps[timelineSteps.length - 1]).padStart(2, "0")}`) : "";
    return `<article class="span-item span-depth-${depth} ${timelineSteps.length ? "timeline-linked" : ""} ${span.status === "failed" ? "failed" : ""}" data-span-id="${escapeHtml(span.id)}" data-timeline-steps="${timelineSteps.join(",")}" style="margin-left:${depth * 24}px"><div class="span-sequence"><span>${String(index + 1).padStart(2, "0")}</span></div><div class="span-content"><header><span class="span-kind ${escapeHtml(span.kind)}">${escapeHtml(kindLabels[span.kind] || span.kind.toUpperCase())}</span><b>${escapeHtml(span.name)}</b>${timelineLabel ? `<em class="span-timeline-link">${escapeHtml(timelineLabel)}</em>` : ""}<span class="span-status ${escapeHtml(span.status)}">${span.status === "failed" ? "失败" : "成功"}</span></header>${attrs ? `<div class="span-attributes">${attrs}</div>` : ""}${detailText ? `<p>${escapeHtml(detailText)}</p>` : ""}</div><div class="span-timing"><b>${span.duration_ms ?? 0} ms</b><small>${escapeHtml(time)}</small></div></article>`;
  }).join("") || "<p>这个 Trace 暂无子 Span。</p>";
  document.querySelectorAll(".span-item.timeline-linked").forEach((item) => item.addEventListener("click", () => focusSpanTimeline(item.dataset.timelineSteps.split(",").filter(Boolean).map(Number), item.dataset.spanId)));
  if (focusEventStep !== null) {
    const matching = [...document.querySelectorAll(".span-item.timeline-linked")].filter((item) => item.dataset.timelineSteps.split(",").map(Number).includes(focusEventStep)).sort((left, right) => left.dataset.timelineSteps.split(",").length - right.dataset.timelineSteps.split(",").length)[0];
    if (matching) {
      focusSpanTimeline([focusEventStep], matching.dataset.spanId);
      matching.scrollIntoView({behavior: "smooth", block: "nearest"});
      return;
    }
  }
  document.querySelectorAll(".timeline-item").forEach((item) => item.classList.toggle("trace-active", item.dataset.traceId === traceId));
  const firstLinkedEvent = document.querySelector(`.timeline-item[data-trace-id="${CSS.escape(traceId)}"]`);
  if (firstLinkedEvent) centerTimelineItem(firstLinkedEvent);
}

function correlateTimelineEvents(traces, events) {
  const links = new Map();
  const ranges = new Map(traces.map((trace) => [trace.id, []]));
  events.forEach((event, index) => {
    const eventTime = new Date(event.created_at).getTime();
    const jobId = event.payload?.job_id;
    let trace = jobId ? traces.find((item) => item.metadata?.job_id === jobId) : null;
    if (!trace && Number.isFinite(eventTime)) {
      const candidates = traces.filter((item) => {
        const start = new Date(item.started_at).getTime() - 1000;
        const end = item.ended_at ? new Date(item.ended_at).getTime() + 1000 : Date.now() + 1000;
        return eventTime >= start && eventTime <= end;
      });
      trace = candidates.sort((left, right) => (left.duration_ms ?? Infinity) - (right.duration_ms ?? Infinity))[0] || null;
    }
    if (trace) {
      links.set(index, trace.id);
      ranges.get(trace.id).push(index + 1);
    }
  });
  return {links, ranges};
}

function renderTimeline(events, links = new Map(), taskId = currentTask?.id || null) {
  const timeline = $("#timeline");
  const previousScrollLeft = timeline.scrollLeft;
  const latestEvent = events.length
    ? String(events[events.length - 1].id ?? `${events.length}:${events[events.length - 1].created_at}`)
    : null;
  const shouldFollowLatest = taskId !== renderedTimelineTaskId || latestEvent !== renderedTimelineLatestEvent;
  timeline.innerHTML = events.map((event, index) => {
    const traceId = links.get(index);
    return `<button type="button" class="timeline-item ${traceId ? "trace-linked" : ""}" data-step="${index + 1}" ${traceId ? `data-trace-id="${escapeHtml(traceId)}"` : ""}><span>${String(index + 1).padStart(2, "0")}</span><div><b>${escapeHtml(event.message)}</b>${traceId ? '<em>属于执行记录</em>' : ""}<small>${new Date(event.created_at).toLocaleString()}</small></div></button>`;
  }).join("");
  if (shouldFollowLatest) {
    timeline.scrollLeft = timeline.scrollWidth;
  } else {
    timeline.scrollLeft = Math.min(previousScrollLeft, Math.max(0, timeline.scrollWidth - timeline.clientWidth));
  }
  renderedTimelineTaskId = taskId;
  renderedTimelineLatestEvent = latestEvent;
  document.querySelectorAll(".timeline-item[data-trace-id]").forEach((item) => item.addEventListener("click", () => showTrace(item.dataset.traceId, Number(item.dataset.step)).catch((error) => notify(error.message))));
}

async function renderObservability(taskId) {
  const [metrics, traces, checkpoints, events] = await Promise.all([
    api("/api/metrics"), api(`/api/tasks/${taskId}/traces`), api(`/api/tasks/${taskId}/checkpoints`), api(`/api/tasks/${taskId}/events`),
  ]);
  const evaluation = metrics.latest_evaluation;
  const cards = [
    ["Trace", metrics.trace_count], ["成功率", `${metrics.success_rate}%`],
    ["平均耗时", `${metrics.average_duration_ms} ms`], ["LLM 调用", metrics.llm_calls],
    ["工具调用", metrics.tool_calls], ["Golden Score", evaluation ? `${evaluation.score}%` : "未运行"],
  ];
  $("#metric-cards").innerHTML = cards.map(([label, value]) => `<div class="metric-card"><small>${escapeHtml(label)}</small><b>${escapeHtml(value)}</b></div>`).join("");
  renderRagComparison(evaluation?.retrieval_comparison);
  const correlation = correlateTimelineEvents(traces, events);
  traceTimelineLinks = correlation.links;
  observabilityTimelineEvents = events;
  renderTimeline(events, traceTimelineLinks, taskId);
  $("#trace-list").innerHTML = traces.map((trace) => {
    const steps = correlation.ranges.get(trace.id) || [];
    const range = steps.length ? (steps.length === 1 ? `步骤 ${String(steps[0]).padStart(2, "0")}` : `步骤 ${String(steps[0]).padStart(2, "0")}–${String(steps[steps.length - 1]).padStart(2, "0")}`) : "暂无关联步骤";
    return `<button type="button" class="trace-item" data-trace-id="${escapeHtml(trace.id)}"><span><b>${escapeHtml(trace.name)}</b><br><small>${escapeHtml(trace.kind)} · ${new Date(trace.started_at).toLocaleString()}</small><em class="trace-step-range">${escapeHtml(range)}</em></span><small>${trace.duration_ms ?? 0} ms</small></button>`;
  }).join("") || "<p>尚无执行 Trace。</p>";
  document.querySelectorAll(".trace-item").forEach((item) => item.addEventListener("click", () => showTrace(item.dataset.traceId).catch((error) => notify(error.message))));
  $("#replay-button").disabled = checkpoints.length === 0;
  $("#replay-button").dataset.checkpointId = checkpoints.length ? checkpoints[checkpoints.length - 1].id : "";
  if (traces.length) await showTrace(traces[0].id);
  else $("#span-list").innerHTML = "<p>选择产生过 Trace 的任务后查看调用链。</p>";
}

function renderRagComparison(comparison) {
  const panel = $("#rag-comparison");
  if (!comparison) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  const metricRows = [
    ["File Recall@K", "recall_at_k", "%", false],
    ["MRR", "mrr", "%", false],
    ["Hit@K", "hit_at_k", "%", false],
    ["无关文件率", "irrelevant_rate", "%", true],
    ["平均上下文文件", "average_context_files", "", true],
  ];
  const metrics = metricRows.map(([label, key, unit, lowerIsBetter]) => {
    const before = Number(comparison.baseline[key] ?? 0);
    const after = Number(comparison.hybrid[key] ?? 0);
    const delta = Number(comparison.delta[key] ?? 0);
    const improved = lowerIsBetter ? delta < 0 : delta > 0;
    const neutral = delta === 0;
    return `<div class="rag-metric"><small>${escapeHtml(label)}</small><div><span>${escapeHtml(before)}${unit}</span><b>→</b><strong>${escapeHtml(after)}${unit}</strong><em class="${neutral ? "neutral" : improved ? "improved" : "regressed"}">${delta > 0 ? "+" : ""}${escapeHtml(delta)}${unit}</em></div></div>`;
  }).join("");
  const cases = comparison.cases.map((item) => `<tr><td><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.query)}</small></td><td>${item.expected_files.map((path) => `<code>${escapeHtml(path)}</code>`).join("")}</td><td>${item.baseline_files.length ? item.baseline_files.map((path) => `<code>${escapeHtml(path)}</code>`).join("") : '<span class="rag-empty">未召回</span>'}</td><td>${item.hybrid_files.length ? item.hybrid_files.map((path) => `<code>${escapeHtml(path)}</code>`).join("") : '<span class="rag-empty">未召回</span>'}</td></tr>`).join("");
  panel.classList.remove("hidden");
  panel.innerHTML = `<div class="rag-heading"><div><h3>混合 RAG A/B 评测</h3><p>相同 ${comparison.case_count} 个标准 Case · Top ${comparison.k} · 旧关键词基线对比混合语义检索</p></div><span>基线 → 混合 RAG</span></div><div class="rag-metrics">${metrics}</div><details><summary>查看每个 Case 的召回文件</summary><div class="rag-table-wrap"><table><thead><tr><th>Case / 查询</th><th>标准答案</th><th>旧检索</th><th>混合 RAG</th></tr></thead><tbody>${cases}</tbody></table></div></details>`;
}

function renderJob(job) {
  currentJob = job;
  const panel = $("#job-panel");
  panel.classList.toggle("hidden", job.status === "succeeded");
  panel.classList.toggle("job-failed", ["failed", "abandoned"].includes(job.status));
  $("#job-status").textContent = jobStatusLabels[job.status] || job.status;
  $("#job-status").className = `status ${job.status === "failed" ? "failed" : ""}`;
  const stage = job.current_stage ? ` · 当前步骤：${statusLabels[job.current_stage] || job.current_stage}` : "";
  const heartbeat = job.heartbeat_at ? ` · 最后心跳：${new Date(job.heartbeat_at).toLocaleTimeString()}` : "";
  $("#job-summary").textContent = `Job ${job.id} · 尝试 ${job.attempts}/${job.max_attempts}${stage}${heartbeat}${job.error ? ` · ${job.error}` : ""}`;
  $("#cancel-job-button").classList.toggle("hidden", !["queued", "running"].includes(job.status));
  const resumableTask = currentTask && currentTask.id === job.task_id && ["waiting_requirement_approval", "developing", "testing", "change_ready", "reviewing", "changes_requested", "review_repairing"].includes(currentTask.status);
  $("#resume-job-button").classList.toggle("hidden", !["cancelled", "paused"].includes(job.status) || !resumableTask);
}

async function watchJob(job) {
  const generation = ++watchGeneration;
  if (!knownTasks.some((task) => task.id === job.task_id)) {
    await refreshTaskList();
  }
  if (!currentTask || currentTask.id !== job.task_id) {
    const targetTask = await api(`/api/tasks/${job.task_id}`);
    await render(targetTask);
    setView(defaultViewForTask(targetTask));
    renderTaskList();
  }
  renderJob(job);
  const existingEvents = await api(`/api/tasks/${job.task_id}/events`);
  const lastEventId = existingEvents.length ? existingEvents[existingEvents.length - 1].id : 0;
  const stream = new EventSource(`/api/tasks/${job.task_id}/stream?after=${lastEventId}`);
  let refreshPending = false;
  stream.addEventListener("task_event", async () => {
    if (refreshPending || generation !== watchGeneration) return;
    refreshPending = true;
    try {
      const task = await api(`/api/tasks/${job.task_id}`);
      await render(task);
    } catch (error) {
      notify(error.message);
    } finally {
      refreshPending = false;
    }
  });
  try {
    while (generation === watchGeneration) {
      const latest = await api(`/api/jobs/${job.id}`);
      renderJob(latest);
      if (terminalJobStatuses.has(latest.status)) {
        const task = await api(`/api/tasks/${job.task_id}`);
        await refreshTaskList();
        await render(task);
        if (latest.status === "succeeded" && task.status === "waiting_requirement_approval" && task.metadata.baseline_refresh_count) notify("仓库基线已变化，索引和技术方案已更新，请重新审批");
        else if (latest.status === "succeeded") notify("后台开发、MR 和 Code Review 已完成");
        if (latest.status === "failed") notify(`后台执行失败：${latest.error || "未知错误"}`);
        if (latest.status === "cancelled") notify("后台执行已取消");
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 700));
    }
  } finally {
    stream.close();
  }
}

async function render(task) {
  currentTask = task;
  currentRecovery = null;
  $("#recovery-panel").classList.add("hidden");
  closeRecoveryDetails();
  localStorage.setItem("ai-dev-agent-current-task", task.id);
  updateNavigationAvailability();
  $("#task-id").textContent = `TASK / ${task.id}`; $("#task-title").textContent = task.title;
  $("#task-requirement").textContent = task.requirement;
  $("#status-badge").textContent = statusLabels[task.status] || task.status;
  $("#status-badge").className = `status ${task.status}`;
  $("#retry-task-button").classList.toggle("hidden", task.status !== "failed");
  $("#rerun-task-button").classList.toggle("hidden", !["merged", "rejected"].includes(task.status));
  $("#summary").textContent = task.analysis.summary;
  list($("#criteria"), task.analysis.acceptance_criteria); list($("#assumptions"), task.analysis.assumptions);
  $("#approach").textContent = task.technical_plan.approach;
  $("#files").innerHTML = task.technical_plan.affected_files.map((file) => `<code>${escapeHtml(file)}</code>`).join("");
  list($("#steps"), task.technical_plan.implementation_steps); list($("#risks"), task.technical_plan.risks);

  const plannedSteps = task.technical_plan.development_steps || [];
  const stepExecutions = new Map((task.step_executions || []).map((item) => [item.step_id, item]));
  const completedStepCount = [...stepExecutions.values()].filter((item) => item.status === "completed").length;
  $("#development-step-summary").textContent = plannedSteps.length
    ? `已完成 ${completedStepCount}/${plannedSteps.length} · 每一步只允许修改批准文件`
    : "旧任务按单步骤兼容执行";
  $("#development-steps").innerHTML = (plannedSteps.length ? plannedSteps : [{
    id: "legacy-step", title: "完成批准方案", objective: task.technical_plan.approach,
    allowed_files: task.technical_plan.affected_files, acceptance_checks: task.technical_plan.test_plan || [],
  }]).map((step, index) => {
    const execution = stepExecutions.get(step.id) || {status: "pending", changed_files: [], attempt_count: 0};
    const labels = {pending: "待执行", running: "执行中", paused: "已暂停", completed: "已完成", failed: "失败"};
    const files = (step.allowed_files || []).map((file) => `<code>${escapeHtml(file)}</code>`).join("");
    const diffButton = execution.diff
      ? `<button type="button" class="secondary step-diff-button" data-step-id="${escapeHtml(step.id)}">查看步骤 Diff</button>` : "";
    return `<article class="development-step step-${escapeHtml(execution.status)}"><div class="step-head"><span>${String(index + 1).padStart(2, "0")}</span><div><b>${escapeHtml(step.title)}</b><small>${escapeHtml(labels[execution.status] || execution.status)}${execution.attempt_count ? ` · ${execution.attempt_count} 次尝试` : ""}</small></div></div><p>${escapeHtml(step.objective)}</p><div class="step-files">${files}</div>${execution.error ? `<p class="step-error">${escapeHtml(execution.error)}</p>` : ""}${diffButton}</article>`;
  }).join("");
  document.querySelectorAll(".step-diff-button").forEach((button) => button.addEventListener("click", () => {
    const execution = stepExecutions.get(button.dataset.stepId);
    if (!execution?.diff) return;
    renderDiff(execution.diff);
    $("#diff-scope-label").textContent = `${execution.title} 完成时的累计 Diff`;
    $("#current-diff-button").classList.remove("hidden");
    $("#diff").scrollTo({top: 0, behavior: "smooth"});
  }));

  const analysis = task.repository_analysis;
  $("#repo-facts").innerHTML = [
    ["语言", analysis.language], ["框架", analysis.framework || "未识别"],
    ["分支", analysis.default_branch || "非 Git 模板"], ["基线", analysis.head_sha ? analysis.head_sha.slice(0, 8) : "无"],
    ["文件", analysis.file_count], ["测试", analysis.test_command || "待配置"],
  ].map(([label, value]) => `<div><small>${label}</small><b>${escapeHtml(value)}</b></div>`).join("");
  const contextPack = analysis.context_pack;
  $("#code-context").innerHTML = contextPack ? [
    ["核心文件", contextPack.primary_files, `${contextPack.index_mode} · index v${contextPack.index_version}`],
    ["依赖文件", contextPack.dependency_files, "只读调用链上下文"],
    ["相关测试", contextPack.test_files, `基线 ${contextPack.baseline_sha ? contextPack.baseline_sha.slice(0, 8) : "无"}`],
  ].map(([label, paths, detail]) => `<div class="context-group"><b>${label}</b><small>${escapeHtml(detail)}</small>${paths.map((path) => `<code title="${escapeHtml(path)}">${escapeHtml(path)}</code>`).join("") || "<small>未检索到</small>"}</div>`).join("") : "";
  $("#evidence").innerHTML = (task.technical_plan.evidence || []).map((item) => `<div class="evidence"><code>${escapeHtml(item.path)}:${item.line}</code><p>${escapeHtml(item.snippet)}</p><small>${escapeHtml(item.reason)}</small></div>`).join("") || "<p>当前未找到足够的行级证据，需要在开发阶段继续检索。</p>";
  $("#tool-calls").innerHTML = analysis.tool_calls.map((item) => `<span>✓ ${escapeHtml(item.tool)} · ${escapeHtml(item.summary)}</span>`).join("");

  const attempts = task.development_attempts || [];
  $("#development-panel").classList.toggle("hidden", attempts.length === 0);
  $("#attempts-empty").classList.toggle("hidden", attempts.length > 0);
  $("#development-attempts").innerHTML = attempts.map((attempt, index) => {
    const globalAttempt = index + 1;
    const stepAttempt = attempt.step_attempt != null
      ? attempt.step_attempt
      : (attempt.step_title ? attempt.attempt : null);
    const stepLabel = attempt.step_title
      ? `${escapeHtml(attempt.step_title)}${stepAttempt != null ? ` · 步骤内尝试 #${stepAttempt}` : ""}`
      : "任务级执行";
    return `<article class="attempt ${attempt.exit_code === 0 ? "attempt-pass" : "attempt-fail"}"><div><div class="attempt-heading"><b>执行记录 #${globalAttempt}</b><small>${stepLabel}</small></div><span>${attempt.exit_code === 0 ? "PASS" : "FAIL"}</span></div><p class="attempt-summary"><b>本轮修改内容：</b>${escapeHtml(attempt.summary)}</p>${attempt.exit_code === 0 ? "" : `<p class="attempt-analysis"><b>失败原因分析：</b>${escapeHtml(analyzeAttemptFailure(attempt))}</p>`}<small>执行结果：exit ${attempt.exit_code}</small>${renderAttemptFiles(attempt, index)}${renderAttemptToolCalls(attempt)}<details class="attempt-log"><summary>查看执行日志</summary><pre>${escapeHtml(attempt.output)}</pre></details></article>`;
  }).join("");
  attempts.forEach((attempt, index) => {
    if (attempt.diff && $(`#attempt-diff-${index}`)) renderDiff(attempt.diff, `#attempt-diff-${index}`);
  });
  const waitingRisk = task.status === "waiting_risk_approval";
  $("#risk-panel").classList.toggle("hidden", !waitingRisk);
  $("#risk-reasons").innerHTML = (task.metadata.risk_reasons || []).map((reason) => `<p>⚠ ${escapeHtml(reason)}</p>`).join("");

  const [events, recovery] = await Promise.all([
    api(`/api/tasks/${task.id}/events`),
    api(`/api/tasks/${task.id}/recovery`),
  ]);
  task.recovery = recovery;
  renderRecovery(recovery);
  renderTimeline(events, new Map(), task.id);
  $("#approval-panel").classList.toggle("hidden", !["waiting_requirement_approval", "waiting_requirement_input"].includes(task.status));
  const hasResult = Boolean(task.result); $("#result-panel").classList.toggle("hidden", !hasResult);
  if (hasResult) {
    $("#test-output").textContent = task.result.output; $("#mr-title").textContent = task.result.mr_title;
    $("#mr-description").textContent = task.merge_request?.description || task.result.mr_description;
    $("#mr-title").textContent = task.merge_request?.title || task.result.mr_title;
    renderDiff(cumulativeDiff(task));
    renderDiff(cumulativeDiff(task), "#review-diff");
    $("#diff-scope-label").textContent = task.metadata.workspace_diff && task.status === "changes_requested"
      ? "当前工作区累计修改（包含尚未通过的修改）"
      : "当前任务累计修改";
    $("#current-diff-button").classList.add("hidden");
  }
  if (!hasResult) {
    renderDiff("", "#diff");
    renderDiff("", "#review-diff");
  }
  const reviews = task.reviews || [];
  $("#review-panel").classList.toggle("hidden", reviews.length === 0);
  $("#review-rounds").innerHTML = reviews.map((review) => {
    const findings = review.findings.map((item) =>
      '<div class="finding severity-' + escapeHtml(item.severity) + '">' +
      '<b>' + escapeHtml(item.severity) + ' · ' + escapeHtml(item.category) + '</b>' +
      '<code>' + escapeHtml(item.file || "general") + (item.line ? ':' + item.line : '') + '</code>' +
      '<p>' + escapeHtml(item.message) + '</p><small>' + escapeHtml(item.suggestion) + '</small></div>'
    ).join("") || "<p>没有审查意见。</p>";
    const checks = review.deterministic_checks.map((item) => '<span>✓ ' + escapeHtml(item) + '</span>').join("");
    return '<article class="review-round ' + (review.decision === "approved" ? "review-pass" : "review-fail") + '">' +
      '<div><b>第 ' + review.round + ' 轮 · ' + escapeHtml(review.decision) + '</b><span>' +
      review.findings.filter((item) => item.blocking).length + ' blocking</span></div>' +
      '<p>' + escapeHtml(review.summary) + '</p><div class="review-checks">' + checks + '</div>' +
      findings + '</article>';
  }).join("");
  renderReviewGate(task);
  renderUIAcceptance(task);
  const canGiveFeedback = ["waiting_release_approval", "changes_requested", "waiting_merge_approval", "merged"].includes(task.status);
  const showChat = hasResult;
  $("#feedback-panel").classList.toggle("hidden", !showChat);
  $("#development-placeholder").classList.toggle("hidden", showChat || waitingRisk);
  $("#code-feedback").disabled = !canGiveFeedback;
  $("#continue-development-button").disabled = !canGiveFeedback;
  const feedbackRounds = task.metadata.user_feedback_rounds || [];
  const canRollbackFeedback = canGiveFeedback && task.status !== "merged" && feedbackRounds.some((item) =>
    (item.snapshot_id || item.diff) && item.operation !== "rollback" && !item.rolled_back_at
  );
  $("#rollback-feedback-button").disabled = !canRollbackFeedback;
  const inheritedConversation = task.metadata.inherited_conversation || [];
  const followupContext = task.metadata.followup_of
    ? '<div class="chat-message agent"><b>Agent · 后续任务</b><p>此任务从已合入任务 ' + escapeHtml(task.metadata.followup_of) + ' 派生，并已基于默认分支最新代码重新建立工作区。</p></div>' +
      inheritedConversation.map((feedback, index) => '<div class="chat-message user inherited"><b>历史意见 · 第 ' + (index + 1) + ' 轮</b><p>' + escapeHtml(feedback) + '</p></div>').join("")
    : '';
  const initialChat = followupContext + '<div class="chat-message user"><b>最初需求</b><p>' + escapeHtml(task.requirement) + '</p></div>' +
    '<div class="chat-message agent"><b>Agent</b><p>首轮开发已完成。请查看累计 Git Diff；如果不符合要求，可以继续发送修改意见。</p></div>';
  const feedbackStatusLabels = {
    running: "正在修改",
    failed: "修改失败",
    paused: "已暂停",
    waiting_risk_approval: "等待风险审批",
    waiting_release_approval: "修改完成",
    changes_requested: "需要继续调整",
    review_repairing: "CR 修复中",
  };
  const chatMessages = $("#chat-messages");
  const wasNearBottom = isChatNearBottom(chatMessages);
  const chatRevision = JSON.stringify(feedbackRounds);
  const shouldScrollToLatest = showChat && (
    !renderedChatVisible ||
    renderedChatTaskId !== task.id ||
    renderedChatRevision !== chatRevision ||
    wasNearBottom
  );
  chatMessages.innerHTML = initialChat + feedbackRounds.map((item, index) => {
    const files = (item.changed_files || []).map((path) => escapeHtml(path)).join(" · ");
    const diffButton = item.diff ? '<button type="button" class="secondary chat-diff-button" data-feedback-index="' + index + '">查看本轮 Diff</button>' : '';
    const statusLabel = feedbackStatusLabels[item.status] || (item.status ? item.status : "历史记录");
    const agentMessage = item.agent_message || "这条意见来自旧版本，未保存独立的本轮 Diff；请查看累计 Diff。后续对话会记录每轮修改。";
    return '<div class="chat-message user"><b>你 · 第 ' + (index + 1) + ' 轮</b><p>' + escapeHtml(item.feedback) + '</p><small>' + escapeHtml(item.submitted_at ? new Date(item.submitted_at).toLocaleString() : "") + '</small></div>' +
      '<div class="chat-message agent"><b>Agent · ' + escapeHtml(statusLabel) + '</b><p>' + escapeHtml(agentMessage) + '</p>' +
      (files ? '<div class="chat-files">修改文件：' + files + '</div>' : '<div class="chat-files">本轮尚无代码变化</div>') + diffButton + '</div>';
  }).join("");
  renderedChatTaskId = task.id;
  renderedChatRevision = chatRevision;
  renderedChatVisible = showChat;
  if (shouldScrollToLatest) scrollChatToLatest(chatMessages);
  document.querySelectorAll(".chat-diff-button").forEach((button) => button.addEventListener("click", () => {
    const index = Number(button.dataset.feedbackIndex);
    const round = feedbackRounds[index];
    if (!round?.diff) return;
    renderDiff(round.diff);
    $("#diff-scope-label").textContent = `第 ${index + 1} 轮对话产生的修改`;
    $("#current-diff-button").classList.remove("hidden");
    $("#diff").scrollTo({top: 0, behavior: "smooth"});
  }));
  $("#release-gate").classList.toggle("hidden", task.status !== "waiting_release_approval");
  const uiBlocking = (task.ui_acceptance?.checks || []).some((check) => check.blocking && check.status === "failed");
  $("#publish-pr-button").disabled = uiBlocking;
  if (uiBlocking) $("#release-gate").querySelector("p").textContent = "UI 验收存在阻塞问题，请修复并重新验收后再发布。";
  else $("#release-gate").querySelector("p").textContent = "点击发布后直接创建 GitHub Draft Pull Request。";
  const pullRequest = task.remote_pull_request;
  $("#pull-request-panel").classList.toggle("hidden", !pullRequest);
  if (pullRequest) {
    $("#pull-request-title").textContent = `PR #${pullRequest.number} · ${pullRequest.state}`;
    $("#pull-request-meta").textContent = `${pullRequest.head_branch} → ${pullRequest.base_branch}${pullRequest.draft ? " · Draft" : ""}`;
    $("#pull-request-link").href = pullRequest.url;
    $("#merge-pr-button").classList.toggle("hidden", task.status !== "waiting_merge_approval");
  }
  const githubComments = task.metadata.github_review_comments || [];
  $("#github-comments-panel").classList.toggle("hidden", !pullRequest);
  const githubCommentStatusLabels = {
    pending: "待处理", queued: "已入队", waiting_risk_approval: "等待风险审批",
    resolved: "已解决", failed: "处理失败",
  };
  $("#github-comments-list").innerHTML = githubComments.map((comment) => {
    const selectable = ["pending", "failed"].includes(comment.status) && task.status === "waiting_merge_approval";
    const location = comment.path ? comment.path + (comment.line ? ":" + comment.line : "") : "PR conversation";
    const command = comment.command_requested ? '<code>/agent fix</code>' : '';
    const result = comment.result ? '<small>处理结果：' + escapeHtml(comment.result) + '</small>' : '';
    return '<label class="github-comment"><input type="checkbox" class="github-comment-checkbox" value="' + escapeHtml(comment.key) + '" ' + (selectable ? '' : 'disabled') + '><div class="github-comment-body"><div class="github-comment-meta"><small>@' + escapeHtml(comment.author) + ' · ' + escapeHtml(location) + '</small><span class="github-comment-status ' + (comment.status === 'failed' ? 'failed' : '') + '">' + escapeHtml(githubCommentStatusLabels[comment.status] || comment.status) + '</span></div><p>' + escapeHtml(comment.body) + '</p>' + command + result + '</div></label>';
  }).join("") || '<p class="task-list-empty">尚未同步到 Review 评论。</p>';
  $("#sync-github-comments-button").disabled = !pullRequest || task.status === "merged";
  $("#process-github-comments-button").disabled = task.status !== "waiting_merge_approval" || !githubComments.some((item) => ["pending", "failed"].includes(item.status));
  if (currentJob && currentJob.task_id === task.id) renderJob(currentJob);
  if (activeView === "observability") {
    observabilityTaskId = task.id;
    await renderObservability(task.id);
  }
  const knownIndex = knownTasks.findIndex((item) => item.id === task.id);
  if (knownIndex >= 0) knownTasks[knownIndex] = task;
  else knownTasks.unshift(task);
  renderTaskList();
  setView(activeView === "create" ? defaultViewForTask(task) : activeView);
}

$("#replay-button").addEventListener("click", async () => {
  const checkpointId = $("#replay-button").dataset.checkpointId;
  if (!checkpointId || !currentTask) return notify("当前任务还没有可回放检查点");
  try {
    const result = await api(`/api/tasks/${currentTask.id}/checkpoints/${checkpointId}/replay`, {method: "POST"});
    const panel = $("#replay-result");
    panel.classList.remove("hidden"); panel.classList.toggle("invalid", !result.valid);
    panel.innerHTML = `<b>${result.valid ? "Dry Run 通过" : "Dry Run 发现漂移"}</b><p>阶段：${escapeHtml(result.stage)} → ${escapeHtml(result.next_action)}</p><small>${escapeHtml([...result.checks, ...result.warnings].join(" · "))}</small>`;
    await renderObservability(currentTask.id);
  } catch (error) { notify(error.message); }
});
$("#evaluation-button").addEventListener("click", async () => {
  const button = $("#evaluation-button"); button.disabled = true; button.textContent = "评测中…";
  try {
    const result = await api("/api/evaluations/run", {method: "POST"});
    const delta = result.retrieval_comparison?.delta?.recall_at_k;
    notify(`Golden Cases：${result.passed}/${result.total}，得分 ${result.score}${delta == null ? "" : `；RAG Recall@K ${delta >= 0 ? "+" : ""}${delta}%`}`);
    if (currentTask) await renderObservability(currentTask.id);
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.textContent = "运行 Golden + RAG 评测"; }
});

$("#task-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = $("#create-button"); button.disabled = true; button.textContent = "正在分析仓库并生成方案…";
  try {
    const task = await api("/api/tasks", {method: "POST", body: JSON.stringify({
      title: $("#title").value,
      requirement: $("#requirement").value,
      repository_id: $("#repository").value,
      figma_url: $("#figma-url").value.trim(),
      preview_url: $("#preview-url").value.trim(),
      viewport_width: Number($("#viewport-width").value || 1440),
      viewport_height: Number($("#viewport-height").value || 900),
    })});
    await refreshTaskList(); await render(task); notify("仓库分析和技术方案已生成");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.innerHTML = "生成需求分析与技术方案 <b>→</b>"; }
});
$("#new-task-button").addEventListener("click", showNewTask);
$("#empty-new-task-button").addEventListener("click", showNewTask);
document.querySelectorAll(".rail-new-task").forEach((button) => button.addEventListener("click", showNewTask));
$("#cancel-create-button").addEventListener("click", () => setView(currentTask ? "requirement" : "requirement"));
document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  if (button.disabled) return;
  setView(button.dataset.view);
}));
$("#rerun-task-button").addEventListener("click", async () => {
  if (!currentTask) return;
  const sourceId = currentTask.id;
  const button = $("#rerun-task-button");
  button.disabled = true; button.textContent = "正在读取最新基线并重新规划…";
  try {
    const task = await api(`/api/tasks/${sourceId}/rerun`, {method: "POST"});
    await refreshTaskList();
    await render(task);
    notify(`已创建新的重跑任务，来源：${sourceId}`);
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.textContent = "重新执行为新任务"; }
});
$("#retry-task-button").addEventListener("click", async () => {
  if (!currentTask) return;
  const button = $("#retry-task-button");
  button.disabled = true; button.textContent = "正在分析失败上下文…";
  try {
    const job = await api(`/api/tasks/${currentTask.id}/retry`, {method: "POST", body: JSON.stringify({actor: "web-user", comment: "分析最近失败并继续开发"})});
    renderJob(job); notify("已进入失败续跑队列，Agent 会先诊断上次错误再继续");
    await watchJob(job);
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.innerHTML = "分析失败并继续 <b>→</b>"; }
});
$("#register-remote-button").addEventListener("click", async () => {
  const button = $("#register-remote-button");
  button.disabled = true; button.textContent = "正在连接并克隆…";
  try {
    const repository = await api("/api/repositories", {method: "POST", body: JSON.stringify({name: $("#repo-name").value, remote_url: $("#repo-url").value})});
    await loadRepositories(repository.id); notify("GitHub 仓库已连接，可以创建任务");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.textContent = "连接并添加仓库"; }
});

$("#register-local-button").addEventListener("click", async () => {
  const button = $("#register-local-button");
  button.disabled = true;
  try {
    const path = $("#repo-path").value;
    const inferredName = path.split(/[\\/]/).filter(Boolean).at(-1) || "本地仓库";
    const repository = await api("/api/repositories", {method: "POST", body: JSON.stringify({name: $("#repo-name").value || inferredName, local_path: path})});
    await loadRepositories(repository.id); notify("本地仓库添加成功");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; }
});

async function browseRepositoryFolders(path = null) {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  const state = await api(`/api/repository-folders${query}`);
  folderPickerState = state;
  $("#folder-picker-path").textContent = state.path || "允许访问的位置";
  $("#folder-picker-parent").disabled = state.path === null;
  $("#folder-picker-select").disabled = !state.is_git_repository;
  $("#folder-picker-hint").textContent = state.is_git_repository
    ? "当前文件夹是 Git 仓库，可以选择"
    : "请选择一个包含 .git 的文件夹";
  $("#folder-picker-list").innerHTML = state.directories.length
    ? state.directories.map((directory) => `<button type="button" class="folder-picker-item" data-folder-path="${escapeHtml(directory.path)}"><span class="folder-icon">📁</span><span class="folder-copy"><b>${escapeHtml(directory.name)}</b><small>${escapeHtml(directory.path)}</small></span>${directory.is_git_repository ? '<span class="git-badge">GIT</span>' : '<span>›</span>'}</button>`).join("")
    : '<div class="folder-picker-empty">这个文件夹中没有可浏览的子文件夹</div>';
  document.querySelectorAll(".folder-picker-item").forEach((button) => button.addEventListener("click", () => browseRepositoryFolders(button.dataset.folderPath).catch((error) => notify(error.message))));
}

function closeFolderPicker() { $("#folder-picker").classList.add("hidden"); }

$("#open-folder-picker-button").addEventListener("click", async () => {
  $("#folder-picker").classList.remove("hidden");
  try { await browseRepositoryFolders($("#repo-path").value || null); }
  catch (error) { closeFolderPicker(); notify(error.message); }
});
$("#folder-picker-parent").addEventListener("click", () => browseRepositoryFolders(folderPickerState?.parent || null).catch((error) => notify(error.message)));
$("#folder-picker-select").addEventListener("click", () => {
  if (!folderPickerState?.is_git_repository) return;
  $("#repo-path").value = folderPickerState.path;
  if (!$("#repo-name").value.trim()) {
    $("#repo-name").value = folderPickerState.path.split(/[\\/]/).filter(Boolean).at(-1) || "本地仓库";
  }
  closeFolderPicker();
});
document.querySelectorAll(".folder-picker-close, .folder-picker-cancel").forEach((button) => button.addEventListener("click", closeFolderPicker));
$("#folder-picker").addEventListener("click", (event) => { if (event.target === $("#folder-picker")) closeFolderPicker(); });

async function recoverCurrentTask() {
  if (!currentTask || currentRecovery?.status !== "recoverable") return;
  const buttons = [$("#recover-task-button"), $("#recovery-drawer-resume")];
  buttons.forEach((button) => { button.disabled = true; button.textContent = "正在加入恢复队列…"; });
  try {
    const job = await api(`/api/tasks/${currentTask.id}/recovery/resume`, {
      method: "POST",
      body: JSON.stringify({actor: "web-user", comment: "从诊断确认的安全检查点继续"}),
    });
    closeRecoveryDetails();
    renderJob(job);
    notify(currentRecovery?.active_job_stale
      ? "旧执行已终止，恢复任务将从最近安全检查点继续"
      : "恢复任务已入队，将从最近安全检查点继续");
    await watchJob(job);
  } catch (error) {
    notify(error.message);
  } finally {
    buttons.forEach((button) => { button.disabled = false; button.innerHTML = "从检查点继续 <b>→</b>"; });
  }
}

$("#recovery-details-button").addEventListener("click", openRecoveryDetails);
$("#recover-task-button").addEventListener("click", recoverCurrentTask);
$("#recovery-drawer-resume").addEventListener("click", recoverCurrentTask);
$("#recovery-drawer-close").addEventListener("click", closeRecoveryDetails);
$("#recovery-drawer-cancel").addEventListener("click", closeRecoveryDetails);
$("#recovery-drawer").addEventListener("click", (event) => { if (event.target === $("#recovery-drawer")) closeRecoveryDetails(); });

$("#approve-button").addEventListener("click", async () => {
  const button = $("#approve-button"); button.disabled = true; button.textContent = "正在加入队列…";
  try {
    const job = await api(`/api/tasks/${currentTask.id}/approve`, {method: "POST", body: JSON.stringify({actor: "web-user", comment: "方案清晰，同意执行"})});
    renderJob(job); notify("审批已入队，页面会实时更新执行进度");
    await watchJob(job);
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.innerHTML = "批准方案 <b>→</b>"; }
});
$("#cancel-job-button").addEventListener("click", async () => {
  if (!currentJob) return;
  try {
    const job = await api(`/api/jobs/${currentJob.id}/cancel`, {method: "POST"});
    renderJob(job);
    notify(job.status === "cancelled" ? "排队任务已取消" : "已请求暂停，将在下一个安全检查点生效");
  } catch (error) { notify(error.message); }
});
$("#resume-job-button").addEventListener("click", async () => {
  if (!currentJob) return;
  const button = $("#resume-job-button"); button.disabled = true; button.textContent = "正在恢复…";
  try {
    const job = await api(`/api/jobs/${currentJob.id}/resume`, {method: "POST", body: JSON.stringify({actor: "web-user", comment: "继续上次取消的执行"})});
    renderJob(job); notify("已创建续跑任务，将从最近可恢复阶段继续");
    await watchJob(job);
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.innerHTML = "继续执行 <b>→</b>"; }
});
$("#revise-button").addEventListener("click", async () => {
  const feedback = $("#revision-feedback").value.trim(); if (!feedback) return notify("请先填写修改意见");
  try {
    const task = await api(`/api/tasks/${currentTask.id}/revise`, {method: "POST", body: JSON.stringify({actor: "web-user", feedback})});
    await render(task); notify("方案已根据意见重新生成");
  } catch (error) { notify(error.message); }
});
$("#reject-button").addEventListener("click", async () => {
  try {
    const task = await api(`/api/tasks/${currentTask.id}/reject`, {method: "POST", body: JSON.stringify({actor: "web-user", comment: "需要调整方案"})});
    await render(task); notify("方案已拒绝");
  } catch (error) { notify(error.message); }
});
$("#risk-approve-button").addEventListener("click", async () => {
  try {
    const task = await api(`/api/tasks/${currentTask.id}/risk/approve`, {method: "POST", body: JSON.stringify({actor: "web-user", comment: "已确认高风险范围"})});
    await render(task); notify("高风险修改已审批，执行继续");
  } catch (error) { notify(error.message); }
});
$("#risk-reject-button").addEventListener("click", async () => {
  try {
    const task = await api(`/api/tasks/${currentTask.id}/risk/reject`, {method: "POST", body: JSON.stringify({actor: "web-user", comment: "拒绝高风险修改"})});
    await render(task); notify("高风险修改已拒绝");
  } catch (error) { notify(error.message); }
});
$("#rerun-review-button").addEventListener("click", async () => {
  const button = $("#rerun-review-button");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "正在重新审查…";
  try {
    const task = await api("/api/tasks/" + currentTask.id + "/review/run", {method: "POST"});
    await render(task); notify("Code Review 已重新执行");
  } catch (error) { notify(error.message); }
  finally {
    renderReviewGate(currentTask);
  }
});
$("#rerun-ui-acceptance-button").addEventListener("click", async () => {
  if (!currentTask?.design_reference?.preview_url) return notify("请先为任务配置可访问的实现页面地址");
  const button = $("#rerun-ui-acceptance-button");
  button.disabled = true; button.textContent = "正在验收…";
  try {
    const task = await api(`/api/tasks/${currentTask.id}/ui-acceptance`, {method: "POST"});
    await render(task); notify("UI 验收已完成");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.textContent = "重新验收"; }
});
$("#approve-review-button").addEventListener("click", async () => {
  try {
    const task = await api("/api/tasks/" + currentTask.id + "/review/approve", {method: "POST", body: JSON.stringify({actor: "web-user", comment: "人工确认剩余风险可接受"})});
    await render(task); notify("Code Review 已人工批准");
  } catch (error) { notify(error.message); }
});
$("#continue-development-button").addEventListener("click", async () => {
  if (!currentTask) return;
  const feedback = $("#code-feedback").value.trim();
  if (!feedback) return notify("请先填写对当前代码的修改意见");
  const button = $("#continue-development-button");
  button.disabled = true; button.textContent = "正在加入继续开发队列…";
  try {
    const job = await api(`/api/tasks/${currentTask.id}/feedback`, {
      method: "POST",
      body: JSON.stringify({actor: "web-user", feedback}),
    });
    $("#code-feedback").value = "";
    renderJob(job);
    notify("修改意见已提交，Agent 会基于当前工作区继续开发");
    await watchJob(job);
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.innerHTML = "发送并继续开发 <b>→</b>"; }
});
$("#rollback-feedback-button").addEventListener("click", async () => {
  if (!currentTask) return;
  const button = $("#rollback-feedback-button");
  button.disabled = true; button.textContent = "正在撤销…";
  try {
    const job = await api(`/api/tasks/${currentTask.id}/feedback`, {
      method: "POST",
      body: JSON.stringify({actor: "web-user", feedback: "撤销上一轮修改"}),
    });
    renderJob(job);
    notify("撤销请求已提交，将恢复上一轮开始前的代码");
    await watchJob(job);
  } catch (error) { notify(error.message); }
  finally { button.textContent = "撤销上一轮"; }
});
$("#current-diff-button").addEventListener("click", () => {
  if (!currentTask?.result) return;
  renderDiff(cumulativeDiff(currentTask));
  $("#diff-scope-label").textContent = currentTask.metadata.workspace_diff && currentTask.status === "changes_requested"
    ? "当前工作区累计修改（包含尚未通过的修改）"
    : "当前任务累计修改";
  $("#current-diff-button").classList.add("hidden");
});
$("#reject-release-button").addEventListener("click", async () => {
  try {
    const task = await api("/api/tasks/" + currentTask.id + "/release/reject", {method: "POST", body: JSON.stringify({actor: "web-user", comment: "暂不进入发布阶段"})});
    await render(task); notify("已拒绝进入发布阶段");
  } catch (error) { notify(error.message); }
});
$("#publish-pr-button").addEventListener("click", async () => {
  const button = $("#publish-pr-button"); button.disabled = true; button.textContent = "正在发布…";
  try {
    const task = await api(`/api/tasks/${currentTask.id}/pull-request/publish`, {method: "POST", body: JSON.stringify({actor: "web-user", comment: "确认发布 GitHub PR"})});
    await render(task); notify("GitHub Draft PR 已创建，等待人工合入");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.innerHTML = "发布 GitHub PR <b>→</b>"; }
});
$("#refresh-pr-button").addEventListener("click", async () => {
  try {
    const task = await api(`/api/tasks/${currentTask.id}/pull-request/refresh`, {method: "POST"});
    await render(task); notify("GitHub PR 状态已同步");
  } catch (error) { notify(error.message); }
});
$("#sync-github-comments-button").addEventListener("click", async () => {
  if (!currentTask?.remote_pull_request) return;
  const button = $("#sync-github-comments-button"); button.disabled = true; button.textContent = "同步中…";
  try {
    const result = await api(`/api/tasks/${currentTask.id}/pull-request/comments/sync`, {method: "POST"});
    await render(result.task);
    if (result.job) {
      renderJob(result.job);
      notify("检测到 /agent fix，已自动进入修复队列");
      await watchJob(result.job);
    } else {
      notify("GitHub PR 评论已同步");
    }
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.textContent = "同步评论"; }
});
$("#process-github-comments-button").addEventListener("click", async () => {
  if (!currentTask) return;
  const commentKeys = Array.from(document.querySelectorAll(".github-comment-checkbox:checked")).map((item) => item.value);
  if (!commentKeys.length) return notify("请先选择至少一条待处理评论");
  const button = $("#process-github-comments-button"); button.disabled = true; button.textContent = "正在加入队列…";
  try {
    const job = await api(`/api/tasks/${currentTask.id}/pull-request/comments/process`, {
      method: "POST",
      body: JSON.stringify({actor: "web-user", comment_keys: commentKeys}),
    });
    renderJob(job);
    notify("Review 意见已交给 Agent 处理");
    await watchJob(job);
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.innerHTML = "交给 Agent 处理 <b>→</b>"; }
});
$("#merge-pr-button").addEventListener("click", async () => {
  if (!window.confirm("确认将这个 Pull Request 合入目标分支？该操作会修改远端仓库。")) return;
  const button = $("#merge-pr-button"); button.disabled = true; button.textContent = "正在合入…";
  try {
    const task = await api(`/api/tasks/${currentTask.id}/pull-request/merge`, {method: "POST", body: JSON.stringify({actor: "web-user", comment: "人工确认合入"})});
    await render(task); notify("GitHub Pull Request 已合入");
  } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.innerHTML = "确认合入 <b>→</b>"; }
});
async function initialize() {
  await loadRepositories();
  await refreshTaskList();
  const savedTaskId = localStorage.getItem("ai-dev-agent-current-task");
  if (savedTaskId) {
    try { await selectTask(savedTaskId); return; }
    catch (_error) { localStorage.removeItem("ai-dev-agent-current-task"); }
  }
  currentTask = null;
  setView("requirement");
  renderTaskList();
}

initialize().catch((error) => notify(error.message));
