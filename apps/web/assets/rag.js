/* 人工标注闭环页面：独立于任务轮询，未确认标签绝不参与评测。 */
let ragData = {cases: [], latest_run: null};
let ragSelected = null;
let ragDirty = false;
const ragEl = (id) => document.getElementById(id);
const ragStates = {pending: "待确认", confirmed: "已确认", excluded: "已排除"};
const ragLabels = {unreviewed: "未审核", required: "必要文件", supporting: "辅助上下文", irrelevant: "无关 / 新增文件（开发前不可检索）"};

async function loadRagDataset() {
  if (ragDirty && !confirm("刷新会放弃尚未保存的标注，继续吗？")) return;
  const [data, repositories] = await Promise.all([api("/api/rag/dataset"), api("/api/repositories")]);
  ragData = data;
  const selected = ragEl("rag-repository").value;
  ragEl("rag-repository").innerHTML = '<option value="">全部仓库</option>' + repositories.map((repo) => `<option value="${escapeHtml(repo.id)}">${escapeHtml(repo.name)}</option>`).join("");
  ragEl("rag-repository").value = selected;
  ragEl("rag-counts").textContent = `待确认 ${data.counts.pending} · 已确认 ${data.counts.confirmed} · 已排除 ${data.counts.excluded}`;
  renderRagList();
  if (ragSelected) renderRagEditor(ragSelected);
  renderRagReport(data.latest_run);
}

function renderRagList() {
  const filter = ragEl("rag-filter").value;
  const repo = ragEl("rag-repository").value;
  const cases = ragData.cases.filter((item) => (filter === "all" || item.state === filter) && (!repo || item.repository_id === repo));
  ragEl("rag-case-list").innerHTML = cases.map((item) => `<button type="button" class="rag-case secondary ${item.id === ragSelected ? "selected" : ""}" data-case="${escapeHtml(item.id)}"><b>${escapeHtml(item.title)}</b><span>${escapeHtml(item.query)}</span><small>${ragStates[item.state]} · ${escapeHtml(item.repository_name)} · ${item.split === "test" ? "测试集" : "开发集"}</small></button>`).join("") || '<p>暂无样本。可以先提取候选，或切换筛选条件。</p>';
  ragEl("rag-case-list").querySelectorAll("[data-case]").forEach((button) => button.addEventListener("click", () => {
    if (ragDirty && ragSelected !== button.dataset.case && !confirm("切换样本会放弃尚未保存的标注，继续吗？")) return;
    ragSelected = button.dataset.case;
    renderRagList();
    renderRagEditor(ragSelected);
  }));
}

function ragLabelRow(item) {
  return `<tr><td><code>${escapeHtml(item.path)}</code><small>${escapeHtml(item.sources.join(" / "))}${!item.exists_at_base ? " · 开发前不存在" : !item.indexable ? " · 当前不索引此类型" : ""}</small></td><td><select class="rag-file-label" data-path="${escapeHtml(item.path)}" aria-label="${escapeHtml(item.path)} 的相关性">${Object.entries(ragLabels).map(([value, label]) => `<option value="${value}" ${item.label === value ? "selected" : ""} ${["required", "supporting"].includes(value) && (!item.exists_at_base || !item.indexable) ? "disabled" : ""}>${label}</option>`).join("")}</select></td></tr>`;
}

function renderRagEditor(id) {
  const item = ragData.cases.find((value) => value.id === id);
  if (!item) return;
  ragDirty = false;
  ragEl("rag-editor").innerHTML = `<h2>${escapeHtml(item.title)}</h2><p>${ragStates[item.state]} · 标注版本 ${item.revision} · 基线 <code>${escapeHtml(item.base_sha.slice(0, 12))}</code></p><p>必要：实现需求必须阅读的既有文件；辅助：有助于理解的上下文；无关：不应召回。新增文件不能作为开发前检索答案，可补充它关联的既有入口。</p>${item.pr_url && /^https:\/\/github\.com\//.test(item.pr_url) ? `<p><a href="${escapeHtml(item.pr_url)}" target="_blank" rel="noopener noreferrer">查看来源 PR #${item.pr_number}</a></p>` : ""}${item.warnings.map((warning) => `<p class="rag-warning">${escapeHtml(warning)}</p>`).join("")}<label>评测需求（只描述需求，不要透露文件答案）<textarea id="rag-query" rows="4">${escapeHtml(item.query)}</textarea></label><div class="rag-toolbar"><label>标注人<input id="rag-actor" maxlength="80" value="${escapeHtml(item.actor || localStorage.getItem("rag-actor") || "")}" placeholder="填写姓名或昵称"></label><label>分组<select id="rag-split"><option value="dev" ${item.split === "dev" ? "selected" : ""}>开发集（调参）</option><option value="test" ${item.split === "test" ? "selected" : ""}>测试集（保留验收）</option></select></label></div><div class="rag-table-scroll"><table class="rag-table"><thead><tr><th>候选文件及来源</th><th>人工标签</th></tr></thead><tbody id="rag-labels">${item.candidates.map(ragLabelRow).join("")}</tbody></table></div><div class="rag-toolbar"><button type="button" class="secondary" id="rag-load-files">加载基线文件（补充漏标）</button><select id="rag-extra-file" aria-label="选择遗漏文件"><option value="">先加载基线文件</option></select><button type="button" class="secondary" id="rag-add-file">添加候选</button></div><label>标注说明<textarea id="rag-note" rows="2">${escapeHtml(item.note)}</textarea></label><div class="rag-toolbar"><button type="button" data-save="pending" class="secondary">保存草稿</button><button type="button" data-save="confirmed">确认标注</button><button type="button" data-save="excluded" class="secondary">排除此样本</button></div><p id="rag-editor-status" role="status"></p>`;
  ragEl("rag-load-files").addEventListener("click", (event) => ragBusy(event.currentTarget, async () => {
    const data = await api(`/api/rag/dataset/cases/${id}/files`);
    ragEl("rag-extra-file").innerHTML = data.files.map((path) => `<option value="${escapeHtml(path)}">${escapeHtml(path)}</option>`).join("");
  }));
  ragEl("rag-editor").oninput = () => { ragDirty = true; };
  ragEl("rag-editor").onchange = () => { ragDirty = true; };
  ragEl("rag-add-file").addEventListener("click", () => {
    const path = ragEl("rag-extra-file").value;
    if (!path || [...ragEl("rag-labels").querySelectorAll("select")].some((input) => input.dataset.path === path)) return;
    ragEl("rag-labels").insertAdjacentHTML("beforeend", ragLabelRow({path, sources: ["human_added"], exists_at_base: true, indexable: true, label: "unreviewed"}));
    ragDirty = true;
  });
  ragEl("rag-editor").querySelectorAll("[data-save]").forEach((button) => button.addEventListener("click", () => ragBusy(button, async () => {
    const labels = Object.fromEntries([...ragEl("rag-labels").querySelectorAll("select")].map((input) => [input.dataset.path, input.value]));
    const body = {revision: item.revision, query: ragEl("rag-query").value, actor: ragEl("rag-actor").value, split: ragEl("rag-split").value, note: ragEl("rag-note").value, state: button.dataset.save, labels};
    await api(`/api/rag/dataset/cases/${id}`, {method: "PUT", body: JSON.stringify(body)});
    localStorage.setItem("rag-actor", body.actor);
    ragDirty = false;
    await loadRagDataset();
    notify("标注已保存；只有已确认样本参与评测");
  })));
}

function renderRagReport(report) {
  if (!report) { ragEl("rag-report").innerHTML = "<p>尚未运行人工标注数据集评测。请先确认样本；不会用候选自动生成分数。</p>"; return; }
  const names = {recall_at_k: "相关文件 Recall@K", required_recall_at_k: "必要文件 Recall@K", precision_at_k: "Precision@K", mrr: "MRR", hit_at_k: "Hit@K", irrelevant_rate: "未标相关文件占比", average_context_files: "平均上下文文件数"};
  const bm25Baseline = (report.baseline_strategy || "").startsWith("bm25f_");
  const baselineLabel = bm25Baseline ? "BM25F + 哈希词向量 + RRF" : "旧关键词 + 哈希词向量 + RRF";
  const baselineShort = bm25Baseline ? "BM25F" : "旧检索";
  const metricTable = (title, baseline, hybrid, delta) => {
    if (!baseline) return "";
    const rows = Object.entries(names).map(([key, label]) => `<tr><td>${label}</td><td>${baseline[key]}</td><td>${hybrid[key]}</td><td>${delta[key] > 0 ? "+" : ""}${delta[key]}</td></tr>`).join("");
    return `<h3>${title}</h3><div class="rag-table-scroll"><table class="rag-table"><thead><tr><th>指标（百分比，文件数除外）</th><th>${baselineLabel}</th><th>BM25F + Embedding（Chroma）+ RRF</th><th>变化（百分点）</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  };
  const rawTable = metricTable("原始检索 Top K", report.baseline, report.hybrid, report.delta);
  const contextTable = metricTable("Agent 最终上下文 Top K（候选扩大 + 依赖扩展 + 文件角色重排）", report.baseline_context, report.hybrid_context, report.context_delta);
  const timing = report.baseline ? `<p>原始检索平均耗时：基线 ${report.average_query_ms.baseline} ms → RAG ${report.average_query_ms.hybrid} ms（不含首次建库）。${report.average_final_context_ms ? ` 最终上下文：基线 ${report.average_final_context_ms.baseline} ms → RAG ${report.average_final_context_ms.hybrid} ms。` : ""}</p>` : "";
  const cases = report.cases.map((item) => `<div class="rag-case-detail"><b>${escapeHtml(item.title)}</b><p>基线 ${escapeHtml(item.base_sha.slice(0, 12))} · 标注 v${item.annotation_revision}</p><p>标准文件：${escapeHtml(item.expected_files.join("，"))}</p><p>原始 ${baselineShort}：${escapeHtml(item.baseline_files.join("，"))}</p><p>原始 RAG：${escapeHtml(item.hybrid_files.join("，"))}</p>${item.baseline_context_files ? `<p>最终 ${baselineShort}：${escapeHtml(item.baseline_context_files.join("，"))}</p><p>最终 RAG：${escapeHtml(item.hybrid_context_files.join("，"))}</p>` : ""}</div>`).join("");
  ragEl("rag-report").innerHTML = `<p>${escapeHtml(new Date(report.created_at).toLocaleString())} · ${report.split === "test" ? "测试集" : "开发集"} · Top ${report.k} · 成功评测 ${report.evaluated_count}/${report.requested_count} · ${escapeHtml(report.status)} · 模型 ${escapeHtml(report.embedding_model || "未就绪")}</p>${rawTable || "<p>没有成功评测的样本，未生成分数。</p>"}${contextTable}${timing}<p>${escapeHtml(report.limitations)}</p>${report.errors.map((error) => `<p class="rag-warning">未计分：${escapeHtml(error.title)} — ${escapeHtml(error.reason)}</p>`).join("")}<details><summary>查看逐条召回及标注版本</summary>${cases}</details>`;
}

async function ragBusy(button, operation) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "执行中…";
  try { await operation(); } catch (error) { notify(error.message); }
  finally { button.disabled = false; button.textContent = original; }
}

ragEl("rag-filter").addEventListener("change", renderRagList);
ragEl("rag-repository").addEventListener("change", renderRagList);
ragEl("rag-refresh").addEventListener("click", (event) => ragBusy(event.currentTarget, loadRagDataset));
ragEl("rag-import").addEventListener("click", (event) => ragBusy(event.currentTarget, async () => {
  const data = await api("/api/rag/dataset/import", {method: "POST", body: JSON.stringify({repository_id: ragEl("rag-repository").value || null, merged_only: ragEl("rag-source").value === "merged", limit: Number(ragEl("rag-limit").value)})});
  ragEl("rag-import-result").innerHTML = `<p>新增 ${data.created.length} · 已存在 ${data.skipped.length} · 失败 ${data.errors.length}。${escapeHtml(data.message)}</p>${data.errors.map((error) => `<p class="rag-warning">${escapeHtml(error.title)}：${escapeHtml(error.reason)}</p>`).join("")}`;
  await loadRagDataset();
}));
ragEl("rag-import-cfe-unit").addEventListener("click", (event) => ragBusy(event.currentTarget, async () => {
  const data = await api("/api/rag/dataset/import-builtin", {method: "POST", body: JSON.stringify({repository_id: ragEl("rag-repository").value || null})});
  ragEl("rag-import-result").innerHTML = `<p>cfe-unit 测试集：新增 ${data.created.length} · 已存在 ${data.skipped.length} · 失败 ${data.errors.length}。${escapeHtml(data.message)}</p>${data.errors.map((error) => `<p class="rag-warning">${escapeHtml(error.requirement_id || "样本")}：${escapeHtml(error.reason)}</p>`).join("")}`;
  ragDirty = false;
  await loadRagDataset();
  ragEl("rag-repository").value = data.repository_id;
  ragEl("rag-filter").value = "confirmed";
  ragEl("rag-eval-split").value = "test";
  renderRagList();
}));
ragEl("rag-evaluate").addEventListener("click", (event) => ragBusy(event.currentTarget, async () => {
  ragEl("rag-report").innerHTML = "<p>正在导出开发前快照并运行评测，首次生成 Embedding 可能需要一段时间…</p>";
  try {
    const report = await api("/api/rag/dataset/evaluate", {method: "POST", body: JSON.stringify({split: ragEl("rag-eval-split").value, k: Number(ragEl("rag-k").value), repository_id: ragEl("rag-repository").value || null})});
    ragData.latest_run = report;
    renderRagReport(report);
  } catch (error) { renderRagReport(ragData.latest_run); throw error; }
}));
