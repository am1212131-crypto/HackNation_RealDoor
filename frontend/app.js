(() => {
  "use strict";

  const API = ""; // same origin
  let sessionId = null;
  let corpus = null;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function announce(msg) {
    $("#status-live").textContent = msg;
  }

  async function api(path, opts = {}) {
    const res = await fetch(API + path, {
      headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : undefined,
      ...opts,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || detail; } catch (e) {}
      throw new Error(detail);
    }
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return res;
  }

  // ---------------------------------------------------------------------
  // Session bootstrap
  // ---------------------------------------------------------------------
  async function initSession() {
    const data = await api("/api/session", { method: "POST" });
    sessionId = data.session_id;
    announce("New session started. Nothing is saved to disk; you can delete this session at any time.");
  }

  // ---------------------------------------------------------------------
  // Stage navigation
  // ---------------------------------------------------------------------
  function showStage(name) {
    for (const s of ["profile", "understand", "prepare"]) {
      $("#stage-" + s).hidden = s !== name;
      $("#nav-" + s).removeAttribute("aria-current");
    }
    $("#nav-" + name).setAttribute("aria-current", "step");
    $("#stage-" + name).querySelector("h2").focus?.();
  }

  $("#nav-profile").addEventListener("click", () => showStage("profile"));
  $("#nav-understand").addEventListener("click", () => showStage("understand"));
  $("#nav-prepare").addEventListener("click", () => showStage("prepare"));

  // ---------------------------------------------------------------------
  // Stage 1: Profile
  // ---------------------------------------------------------------------
  $("#consent-checkbox").addEventListener("change", async (e) => {
    if (e.target.checked) {
      await api(`/api/session/${sessionId}/consent`, {
        method: "POST",
        body: JSON.stringify({ consent_text: "Renter consented to allowlisted extraction from an uploaded synthetic document." }),
      });
      announce("Consent recorded.");
    }
  });

  let households = [];

  async function loadHouseholds() {
    const data = await api("/api/households");
    households = data.households;
    const select = $("#household-select");
    select.innerHTML = '<option value="">Select a household…</option>';
    for (const h of households) {
      const opt = document.createElement("option");
      opt.value = h.household_id;
      opt.textContent = `${h.household_id} — ${h.summary} (household of ${h.household_size})`;
      select.appendChild(opt);
    }
  }

  let currentHousehold = null;

  $("#household-select").addEventListener("change", () => {
    currentHousehold = households.find((h) => h.household_id === $("#household-select").value) || null;
    renderHouseholdDocs(currentHousehold);
    $("#upload-all-result").innerHTML = "";
    if (currentHousehold) {
      $("#household-summary").textContent =
        `Scenario: ${currentHousehold.scenario}. Household size ${currentHousehold.household_size}. ` +
        `Upload documents one by one below, or use "Upload all" to load and confirm-ready all ${currentHousehold.documents.length} at once.`;
      $("#household-size").value = String(currentHousehold.household_size);
      $("#upload-all-btn").hidden = false;
    } else {
      $("#household-summary").textContent = "";
      $("#upload-all-btn").hidden = true;
    }
  });

  $("#upload-all-btn").addEventListener("click", () => uploadAllHouseholdDocs(currentHousehold));

  async function uploadAllHouseholdDocs(hh) {
    if (!hh) return;
    if (!requireConsent()) return;
    const resultEl = $("#upload-all-result");
    const btn = $("#upload-all-btn");
    btn.disabled = true;
    const results = [];
    for (const doc of hh.documents) {
      announce(`Uploading ${doc.doc_type_label}…`);
      const outcome = await uploadHouseholdDoc(hh.household_id, doc.filename, doc.doc_type, { skipConsentCheck: true });
      results.push({ label: doc.doc_type_label, ...outcome });
    }
    btn.disabled = false;
    const okCount = results.filter((r) => r.ok).length;
    resultEl.innerHTML = `<ul>${results.map((r) =>
      `<li>${r.ok ? "✓" : "✕"} ${r.label}${r.ok ? "" : ` — ${r.message}`}</li>`).join("")}</ul>`;
    announce(`Uploaded ${okCount} of ${results.length} documents for ${hh.household_id}. ` +
      (okCount < results.length ? "Some need a document-type pick or the LLM vision fallback — see the list above." : "Now confirm each field below."));
  }

  function renderHouseholdDocs(hh) {
    const list = $("#sample-list");
    list.innerHTML = "";
    if (!hh) return;
    for (const doc of hh.documents) {
      const div = document.createElement("div");
      div.className = "sample-item";
      const span = document.createElement("span");
      let label = doc.doc_type_label;
      if (doc.rasterized) label += " (scanned — needs a document-type pick or AI vision fallback)";
      if (doc.contains_adversarial_text) label += " (contains embedded adversarial text)";
      span.textContent = label;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "Upload this document";
      btn.addEventListener("click", () => uploadHouseholdDoc(hh.household_id, doc.filename, doc.doc_type));
      div.append(span, btn);
      list.appendChild(div);
    }
  }

  async function uploadHouseholdDoc(householdId, filename, docType, opts = {}) {
    if (!opts.skipConsentCheck && !requireConsent()) return { ok: false, message: "Consent not given." };
    const res = await fetch(`/api/households/${householdId}/documents/${filename}`);
    const blob = await res.blob();
    const file = new File([blob], filename, { type: "application/pdf" });
    return doUpload(file, docType);
  }

  function requireConsent() {
    if (!$("#consent-checkbox").checked) {
      announce("Please check the consent box before uploading a document.");
      $("#consent-checkbox").focus();
      return false;
    }
    return true;
  }

  $("#file-input").addEventListener("change", () => {
    $("#upload-btn").disabled = !$("#file-input").files.length;
  });

  $("#upload-btn").addEventListener("click", async () => {
    if (!requireConsent()) return;
    const file = $("#file-input").files[0];
    const docType = $("#doc-type-select").value || null;
    await doUpload(file, docType);
  });

  async function doUpload(file, docType) {
    const fd = new FormData();
    fd.append("file", file);
    if (docType) fd.append("doc_type", docType);
    announce("Uploading and extracting fields…");
    try {
      const result = await api(`/api/session/${sessionId}/upload`, { method: "POST", body: fd });
      announce(`Extracted ${result.fields.length} allowlisted field(s) from ${result.doc_type_display}.`);
      renderDocument(result);
      return { ok: true };
    } catch (e) {
      announce("Upload failed: " + e.message);
      return { ok: false, message: e.message };
    }
  }

  function renderDocument(doc) {
    const container = $("#documents-list");
    const card = document.createElement("section");
    card.className = "doc-card";
    card.dataset.docId = doc.doc_id;
    card.setAttribute("aria-labelledby", "doc-h-" + doc.doc_id);

    const h3 = document.createElement("h3");
    h3.id = "doc-h-" + doc.doc_id;
    h3.textContent = doc.doc_type_display;
    card.appendChild(h3);

    if (doc.injection_flags_detected && doc.injection_flags_detected.length) {
      const banner = document.createElement("div");
      banner.className = "injection-banner";
      banner.setAttribute("role", "alert");
      banner.innerHTML = `<strong>Untrusted-input notice</strong>${doc.injection_flag_note}`;
      card.appendChild(banner);
    }

    const list = document.createElement("div");
    list.className = "field-list";
    for (const f of doc.fields) {
      list.appendChild(renderFieldRow(doc.doc_id, f, doc.doc_type));
    }
    card.appendChild(list);

    container.prepend(card);
  }

  function renderFieldRow(docId, field, docType) {
    const tpl = $("#tpl-field-row").content.cloneNode(true);
    const row = tpl.querySelector(".field-row");
    row.dataset.fieldId = field.field_id;

    row.querySelector(".field-label").textContent = field.label;
    const input = row.querySelector(".field-value-input");
    input.value = field.value || "";
    input.setAttribute("aria-label", field.label + " value");
    if (field.status === "not_found") {
      input.placeholder = "Not found — please enter manually";
    }

    if (field.purpose) {
      const purposeEl = document.createElement("p");
      purposeEl.className = "field-purpose";
      purposeEl.textContent = `Why we ask: ${field.purpose}`;
      row.querySelector(".field-main").insertAdjacentElement("beforebegin", purposeEl);
    }

    row.querySelector(".field-confidence").textContent =
      field.confidence > 0 ? `confidence ${(field.confidence * 100).toFixed(0)}%` : "no auto-match";

    const statusEl = row.querySelector(".field-status");
    const statusText = {
      extracted: "Extracted", not_found: "Needs entry", llm_suggested: "AI-suggested — please verify",
    }[field.status] || "Needs entry";
    statusEl.textContent = statusText;
    statusEl.className = "field-status " + field.status;

    if (field.extraction_method === "llm_assisted") {
      const note = document.createElement("p");
      note.className = "llm-note";
      note.textContent = "This value was suggested by an AI backfill step because the exact field label wasn't found on the page. It has no source box — verify it against your document before confirming.";
      row.querySelector(".field-main").insertAdjacentElement("afterend", note);
    }

    const confirmBtn = row.querySelector(".confirm-field-btn");
    confirmBtn.addEventListener("click", async () => {
      const value = input.value.trim();
      const result = await api(
        `/api/session/${sessionId}/document/${docId}/field/${field.field_id}/confirm`,
        { method: "POST", body: JSON.stringify({ value }) }
      );
      statusEl.textContent = result.field.corrected ? "Confirmed (corrected)" : "Confirmed";
      statusEl.className = "field-status confirmed";
      announce(`${field.label} confirmed${result.field.corrected ? " with your correction" : ""}.`);
      if (result.all_fields_confirmed) {
        announce(`All fields confirmed for this document. You can continue to Understand.`);
      }
      if (docType === "application_summary" && field.field_id === "household_size" && result.field.value) {
        const sizeSelect = $("#household-size");
        const n = parseInt(result.field.value, 10);
        if (n >= 1 && n <= 8) {
          sizeSelect.value = String(n);
          announce(`Household size (${n}) carried over from your confirmed application summary — no need to re-enter it.`);
        }
      }
    });

    const sourceBtn = row.querySelector(".show-source-btn");
    const sourcePanel = row.querySelector(".field-source-panel");
    const panelId = `source-panel-${docId}-${field.field_id}`;
    sourcePanel.id = panelId;
    sourceBtn.setAttribute("aria-controls", panelId);
    sourceBtn.setAttribute("aria-expanded", "false");
    if (!field.source_box) {
      sourceBtn.disabled = true;
      sourceBtn.textContent = field.extraction_method === "llm_assisted"
        ? "No source box (AI-suggested value)" : "No source box (value not found)";
    } else {
      sourceBtn.addEventListener("click", async () => {
        if (!sourcePanel.hidden) {
          sourcePanel.hidden = true;
          sourceBtn.setAttribute("aria-expanded", "false");
          return;
        }
        sourcePanel.hidden = false;
        sourceBtn.setAttribute("aria-expanded", "true");
        if (sourcePanel.dataset.loaded) return;
        const img = await api(`/api/session/${sessionId}/document/${docId}/page-image`);
        renderSourceBox(sourcePanel, img, field);
        sourcePanel.dataset.loaded = "1";
      });
    }

    return row;
  }

  function renderSourceBox(panel, imgData, field) {
    const scale = imgData.resolution_scale;
    const image = document.createElement("img");
    image.src = "data:image/png;base64," + imgData.page_image_b64;
    image.alt = `Page 1 of the document, showing the evidence location for "${field.label}"`;
    panel.appendChild(image);

    image.addEventListener("load", () => {
      const box = document.createElement("div");
      box.className = "field-source-box";
      const b = field.source_box;
      box.style.left = (b.x0 * scale) + "px";
      box.style.top = (b.top * scale) + "px";
      box.style.width = ((b.x1 - b.x0) * scale) + "px";
      box.style.height = ((b.bottom - b.top) * scale) + "px";
      panel.appendChild(box);
    });

    const caption = document.createElement("p");
    caption.className = "field-source-caption";
    caption.textContent = `Source: page ${field.source_box.page}, highlighted region on the document above.`;
    panel.appendChild(caption);
  }

  // ---------------------------------------------------------------------
  // Stage 2: Understand
  // ---------------------------------------------------------------------
  async function loadCorpusMeta() {
    corpus = await api("/api/rules/meta");
    $("#corpus-meta").textContent =
      `${corpus.program_name} — rule year ${corpus.rule_year}, effective ${corpus.effective_date}, ` +
      `event date ${corpus.event_date}. Metro: ${corpus.metro_area}. Scored tier: ${corpus.scored_ami_tier}% AMI. ` +
      `Documents must be dated within ${corpus.document_currency_window_days} days of the event date. Corpus version ${corpus.corpus_version}.`;
  }

  $("#save-profile-btn").addEventListener("click", async () => {
    const householdSize = $("#household-size").value;
    if (!householdSize) {
      announce("Please select a household size.");
      return;
    }
    const flags = {
      has_wage_income: $("#flag-wage").checked,
      has_benefit_income: $("#flag-benefit").checked,
      has_gig_income: $("#flag-gig").checked,
    };
    const householdId = $("#household-select").value || "SELF";
    await api(`/api/session/${sessionId}/profile`, {
      method: "POST",
      body: JSON.stringify({ household_id: householdId, household_size: Number(householdSize), flags }),
    });
    announce("Household info saved.");
  });

  $$(".suggest-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#rule-question").value = btn.dataset.q;
      askQuestion();
    });
  });
  $("#ask-btn").addEventListener("click", askQuestion);

  async function askQuestion() {
    const question = $("#rule-question").value.trim();
    if (!question) return;
    const householdSize = $("#household-size").value ? Number($("#household-size").value) : null;
    const result = await api(`/api/session/${sessionId}/rules/query`, {
      method: "POST",
      body: JSON.stringify({ question, household_size: householdSize, ami_tier: null }),
    });
    const el = $("#rule-answer");
    el.className = result.type;

    const routeBadge = result.route
      ? `<span class="route-badge ${result.route}">${result.route === "rag" ? "📄 Document search (RAG)" : "📊 Structured table lookup"}</span>`
      : "";
    let html = `${routeBadge}<p>${result.message}</p>`;

    if (result.citation) {
      // Structured-table answer: exact-match citation.
      html += `<div class="citation"><strong>Source:</strong> ${result.citation.title}
        (${result.citation.file}), publisher: ${result.citation.publisher}.
        Effective ${result.data.effective_date}${result.data.revised_date ? " (revised " + result.data.revised_date + ")" : ""}.</div>`;
    } else if (result.citations && result.citations.length) {
      // RAG answer: one or more retrieved excerpts, each with a similarity score.
      const rows = result.citations.map(c =>
        `<li>[${c.label}] ${c.file}, page ${c.page} (similarity ${c.similarity})</li>`).join("");
      html += `<div class="citation"><strong>Retrieved excerpts:</strong><ul>${rows}</ul>
        <p class="hint">This answer is generated only from the excerpts above, not from outside knowledge. If it doesn't fully answer your question, ask a human reviewer.</p></div>`;
    }
    el.innerHTML = html;
    announce(result.type === "refusal" ? "Request deflected: RealDoor does not decide eligibility."
      : result.type === "abstain" ? "RealDoor abstained rather than guess."
      : "Answer shown with citation.");
  }

  $("#calc-btn").addEventListener("click", async () => {
    const el = $("#calc-result");
    try {
      const result = await api(`/api/session/${sessionId}/calculation`);
      el.className = result.type;
      if (result.type === "abstain") {
        el.innerHTML = `<p>${result.message}</p>`;
        announce("Calculation abstained: " + result.message);
        return;
      }
      const rows = result.contributions.map((c) =>
        `<li>${c.doc_type}: ${c.formula} = $${c.annualized_usd.toLocaleString()}</li>`).join("");
      const threshold = result.threshold;
      const reasons = result.review_reasons && result.review_reasons.length
        ? `<p><strong>Review reasons:</strong> ${result.review_reasons.join(", ")}</p>` : "";
      const readinessBadge = `<span class="status-badge ${result.readiness_status === "READY_TO_REVIEW" ? "satisfied" : "expired"}">${
        result.readiness_status === "READY_TO_REVIEW" ? "✓ READY_TO_REVIEW" : "⚠ NEEDS_REVIEW"
      }</span>`;
      el.innerHTML = `
        <p><strong>Household:</strong> ${result.household_id}</p>
        <p><strong>Annualized income:</strong> $${Number(result.annualized_income).toLocaleString()}</p>
        <ul>${rows}</ul>
        ${threshold ? `<p><strong>Frozen threshold</strong> (${threshold.ami_tier}, household of
          ${threshold.household_size}): $${threshold.annual_income_limit_usd.toLocaleString()}
          — effective ${threshold.effective_date}</p>` : ""}
        <p><strong>Comparison:</strong> ${result.comparison}</p>
        <p><strong>Readiness status:</strong> ${readinessBadge}</p>
        ${reasons}
        <p class="hint">${result.disclaimer}</p>
        ${threshold ? `<div class="citation"><strong>Source:</strong> ${threshold.source.title} (${threshold.source.file})</div>` : ""}
      `;
      announce(`Calculation complete: ${result.readiness_status}. This is not an eligibility determination.`);
    } catch (e) {
      el.className = "abstain";
      el.innerHTML = `<p>${e.message}</p>`;
    }
  });

  // ---------------------------------------------------------------------
  // Stage 3: Prepare
  // ---------------------------------------------------------------------
  $("#refresh-checklist-btn").addEventListener("click", async () => {
    const result = await api(`/api/session/${sessionId}/checklist`);
    const container = $("#checklist-result");
    container.innerHTML = "";
    for (const item of result.items) {
      const div = document.createElement("div");
      div.className = "checklist-item";
      const badgeText = {
        satisfied: "✓ Satisfied", missing: "✕ Missing", expired: "⏰ Expired", not_applicable: "— Not applicable",
      }[item.status];
      div.innerHTML = `
        <span class="status-badge ${item.status}">${badgeText}</span>
        <strong>${item.label}</strong>
        ${item.status === "expired" ? `<div class="notes">Most recent document dated ${item.most_recent_date}, ${item.age_days} days ago (limit ${result.currency_window_days} days).</div>` : ""}
      `;
      container.appendChild(div);
    }
    announce(`Checklist updated: ${result.items.filter(i => i.status === "missing").length} missing, ` +
      `${result.items.filter(i => i.status === "expired").length} expired.`);
  });

  $("#preview-packet-btn").addEventListener("click", async () => {
    const panel = $("#packet-preview");
    try {
      const preview = await api(`/api/session/${sessionId}/packet/preview`);
      panel.hidden = false;
      const calc = preview.calculation || {};
      const docsRows = preview.documents.map((d) =>
        `<li>${d.doc_type} — ${d.confirmed ? "confirmed" : "NOT yet confirmed (will be left out of the download)"}</li>`).join("");
      const checklistRows = preview.checklist.items.map((i) =>
        `<li>[${i.status}] ${i.label}</li>`).join("");
      panel.innerHTML = `
        <h4>Packet preview</h4>
        <p>Household ${calc.household_id || "(not set)"}: annualized income
          $${(calc.annualized_income ?? 0).toLocaleString()}, comparison ${calc.comparison || "n/a"},
          readiness ${calc.readiness_status || "n/a"}.</p>
        <p><strong>Documents that will be included:</strong></p>
        <ul>${docsRows}</ul>
        <p><strong>Checklist snapshot:</strong></p>
        <ul>${checklistRows}</ul>
        <p class="hint">Not what you expected? Go back to
          <button type="button" class="link-btn" id="preview-edit-profile">1. Profile</button> or
          <button type="button" class="link-btn" id="preview-edit-understand">2. Understand</button>
          to fix it, then preview again.</p>
        <button type="button" id="download-packet-btn">Download packet (.zip)</button>
      `;
      $("#preview-edit-profile").addEventListener("click", () => showStage("profile"));
      $("#preview-edit-understand").addEventListener("click", () => showStage("understand"));
      $("#download-packet-btn").addEventListener("click", downloadPacket);
      announce("Packet preview shown below. Review it, then download when ready.");
    } catch (e) {
      panel.hidden = false;
      panel.innerHTML = `<p>${e.message}</p>`;
      announce("Could not build a preview: " + e.message);
    }
  });

  async function downloadPacket() {
    announce("Preparing your packet…");
    const res = await fetch(`/api/session/${sessionId}/packet`);
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      announce("Could not build packet: " + (j.detail || res.statusText));
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "realdoor_packet.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    announce("Packet downloaded to your device, including copies of your confirmed documents. It was not sent anywhere automatically.");
  }

  $("#view-audit-btn").addEventListener("click", async () => {
    const data = await api(`/api/session/${sessionId}/audit-log`);
    const el = $("#audit-log-view");
    let html = "<table class='audit-log'><caption>Session audit log (actions and rule versions only — never raw document contents)</caption><thead><tr><th>Time</th><th>Action</th></tr></thead><tbody>";
    for (const entry of data.audit_log) {
      html += `<tr><td>${entry.ts}</td><td>${entry.action}</td></tr>`;
    }
    html += "</tbody></table>";
    el.innerHTML = html;
  });

  $("#delete-session-btn").addEventListener("click", () => {
    $("#delete-confirm").hidden = false;
  });
  $("#delete-confirm-no").addEventListener("click", () => {
    $("#delete-confirm").hidden = true;
  });
  $("#delete-confirm-yes").addEventListener("click", async () => {
    await api(`/api/session/${sessionId}`, { method: "DELETE" });
    announce("Your session has been permanently deleted. All uploaded documents and extracted data are gone.");
    document.querySelectorAll("main .stage").forEach(s => s.hidden = true);
    $("#main").innerHTML += "<p><strong>Session deleted.</strong> Reload this page to start a new session.</p>";
  });

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------
  (async function boot() {
    await initSession();
    await loadHouseholds();
    await loadCorpusMeta();
    showStage("profile");
  })();
})();
