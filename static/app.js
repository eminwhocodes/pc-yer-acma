const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

function fmtBytes(n) {
  if (n == null || isNaN(n)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (opts.signal?.aborted) {
    const err = new Error("aborted");
    err.name = "AbortError";
    throw err;
  }
  return res.json();
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderDisk(disk) {
  if (!disk) return;
  const pct = disk.percent || 0;
  const fill = $("#diskFill");
  fill.style.width = `${Math.min(pct, 100)}%`;
  fill.classList.toggle("critical", pct >= 90);
  $("#diskText").textContent = `${fmtBytes(disk.used)} / ${fmtBytes(disk.total)} kullanılıyor · ${fmtBytes(disk.free)} boş (${pct}%)`;
}

function renderAdmin(isAdm) {
  const el = $("#adminBadge");
  if (!el) return;
  el.textContent = isAdm ? "YÖNETİCİ" : "YÖNETİCİ DEĞİL";
  el.className = "admin-badge " + (isAdm ? "ok" : "bad");
}

function fmtAge(seconds) {
  if (seconds == null || isNaN(seconds)) return null;
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} sn önce`;
  if (s < 3600) return `${Math.floor(s / 60)} dk önce`;
  if (s < 86400) return `${Math.floor(s / 3600)} sa önce`;
  return `${Math.floor(s / 86400)} gün önce`;
}

function renderCacheBanner(state) {
  const el = $("#cacheBanner");
  if (!el) return;
  const q = state.quick || {};
  const age = state.cache?.age_seconds;
  const staleAfter = state.cache?.stale_after || 3600;
  if (!q.categories?.length && q.status !== "running") {
    el.className = "banner";
    el.innerHTML = `Henüz kayıtlı tarama yok. İlk tarama başlatılıyor…`;
    return;
  }
  if (q.status === "running") {
    el.className = "banner";
    el.innerHTML = q.from_cache
      ? `Önceki sonuçlar gösteriliyor · <strong>arka planda güncelleniyor…</strong>`
      : `Tarama sürüyor · sonuçlar anlık güncellenir`;
    return;
  }
  if (q.from_cache) {
    const ageTxt = fmtAge(age) || "bilinmiyor";
    const stale = age != null && age > staleAfter;
    el.className = "banner " + (stale ? "warn" : "ok");
    el.innerHTML = `
      Son kayıtlı tarama: <strong>${ageTxt}</strong>
      ${stale ? " · veriler eski sayılır" : " · taze kabul ediliyor (&lt; 1 sa)"}
      <div class="actions"><button type="button" class="btn tiny" id="btnForceScan">Şimdi yenile</button></div>`;
    $("#btnForceScan")?.addEventListener("click", () => api("/api/scan/quick").then(refresh));
    return;
  }
  if (q.status === "done") {
    el.className = "banner ok";
    el.textContent = `Güncel tarama · ${fmtAge(age) || "az önce"}`;
    return;
  }
  el.className = "banner hidden";
}

function renderDockerWarn(state, data) {
  const daemon = data?.daemon || state?.docker_daemon || {};
  const globalEl = $("#dockerWarnGlobal");
  const tabEl = $("#dockerBanner");
  const show =
    daemon.cli === true && daemon.running === false
      ? { title: daemon.message || "Docker çalışmıyor", hint: daemon.hint || "" }
      : daemon.cli === false
        ? { title: daemon.message || "Docker CLI yok", hint: daemon.hint || "" }
        : null;

  for (const el of [globalEl, tabEl]) {
    if (!el) continue;
    if (!show) {
      el.className = "banner warn hidden";
      el.innerHTML = "";
      continue;
    }
    el.className = "banner warn";
    el.innerHTML = `<strong>${escapeHtml(show.title)}</strong><div class="path" style="margin-top:0.25rem">${escapeHtml(show.hint)}</div>`;
  }
}

function renderCategories(state) {
  const q = state.quick || {};
  const cats = q.categories || [];
  const max = Math.max(...cats.map((c) => c.bytes), 1);
  const ageTxt = fmtAge(state.cache?.age_seconds);
  $("#quickStatus").textContent =
    q.status === "running"
      ? `Taranıyor… ${cats.length} kategori`
      : q.status === "done" && q.from_cache
        ? `Önbellek · ${cats.length} kategori${ageTxt ? " · " + ageTxt : ""}`
        : q.status === "done"
          ? `Tamam · ${cats.length} kategori`
          : q.status === "error"
            ? `Hata: ${q.error}`
            : "Bekleniyor";

  renderCacheBanner(state);
  renderDockerWarn(state);

  // Preserve open cards
  const openIds = new Set(
    $$("#catList .card.open").map((c) => c.dataset.id)
  );

  $("#catList").innerHTML = cats
    .map((c) => {
      const pct = Math.round((c.bytes / max) * 100);
      const warn = (c.note || "").startsWith("UYARI");
      const items = (c.items || [])
        .map(
          (it) => `
        <div class="item">
          <div>
            <div>${escapeHtml(it.label || "")}</div>
            <div class="path">${escapeHtml(it.path || "")}</div>
          </div>
          <div class="size">${fmtBytes(it.bytes)}</div>
        </div>`
        )
        .join("");
      const open = openIds.has(c.id) ? " open" : "";
      return `
      <article class="card${open}" data-id="${escapeHtml(c.id)}">
        <div class="card-head">
          <strong>${escapeHtml(c.name)}${warn ? " ⚠" : ""}</strong>
          <span class="size">${fmtBytes(c.bytes)}</span>
        </div>
        <div class="bar-mini"><i style="width:${pct}%"></i></div>
        <p class="note">${escapeHtml(c.note || "")}${c.cleanable ? " · temizlenebilir" : ""}</p>
        <div class="items">${items || "<em>Öğe yok</em>"}</div>
      </article>`;
    })
    .join("");

  $$("#catList .card-head").forEach((el) => {
    el.addEventListener("click", () => el.parentElement.classList.toggle("open"));
  });
}

function renderDeep(state) {
  const d = state.deep || {};
  $("#deepBar").style.width = `${d.progress || 0}%`;
  $("#deepStatus").textContent =
    d.status === "running"
      ? `Taranıyor ${d.progress || 0}% — ${d.current || ""}`
      : d.status === "done"
        ? "Derin tarama tamam"
        : d.status === "error"
          ? `Hata: ${d.error}`
          : "Henüz başlamadı";

  const roots = d.roots || [];
  $("#deepTree").innerHTML = roots
    .slice(0, 40)
    .map((n) => treeNode(n, 0))
    .join("");
}

function renderWorkspaces(state) {
  const w = state.workspaces || {};
  const bar = $("#projBar");
  if (bar) bar.style.width = `${w.progress || 0}%`;
  const st = $("#projStatus");
  if (st) {
    st.textContent =
      w.status === "running"
        ? `Taranıyor ${w.progress || 0}% — ${w.current || ""}`
        : w.status === "done"
          ? w.from_cache
            ? "Önbellek · geliştirme alanları"
            : "Tarama tamam"
          : w.status === "error"
            ? `Hata: ${w.error}`
            : "Henüz taranmadı — Tara’ya bas";
  }

  const roots = w.roots || [];
  const openIds = new Set($$("#projList .card.open").map((c) => c.dataset.id));
  const max = Math.max(...roots.flatMap((r) => (r.projects || []).map((p) => p.bytes)), 1);

  $("#projList").innerHTML = roots
    .map((root) => {
      const projects = root.projects || [];
      const missing = root.missing
        ? `<p class="note">Klasör yok: ${escapeHtml(root.path)}</p>`
        : "";
      const cards = projects
        .map((p) => {
          const pct = Math.round((p.bytes / max) * 100);
          const id = escapeHtml(p.path);
          const open = openIds.has(p.path) ? " open" : "";
          const kids = (p.children || [])
            .map(
              (c) => `
            <div class="item">
              <div>
                <div>${escapeHtml(c.label)}</div>
                <div class="path">${escapeHtml(c.path)}</div>
              </div>
              <div class="size">${fmtBytes(c.bytes)}</div>
            </div>`
            )
            .join("");
          return `
          <article class="card${open}" data-id="${id}">
            <div class="card-head">
              <strong>${escapeHtml(p.label)}</strong>
              <span class="size">${fmtBytes(p.bytes)}</span>
            </div>
            <div class="bar-mini"><i style="width:${pct}%"></i></div>
            <p class="note path">${escapeHtml(p.path)}</p>
            <div class="items">${kids || "<em>Alt kırılım yok (küçük veya ölçülmedi)</em>"}</div>
          </article>`;
        })
        .join("");
      return `
        <div class="workspace-root">
          <h3>${escapeHtml(root.label)} · <span class="size">${fmtBytes(root.bytes || 0)}</span> · ${projects.length} öğe</h3>
          ${missing}
          <div class="cards">${cards || "<p class='hint'>Boş</p>"}</div>
        </div>`;
    })
    .join("");

  $$("#projList .card-head").forEach((el) => {
    el.addEventListener("click", () => el.parentElement.classList.toggle("open"));
  });
}

function treeNode(n, depth) {
  const kids = (n.children || []).map((c) => treeNode(c, depth + 1)).join("");
  return `
    <div class="tree-node" style="padding-left:${depth * 12}px">
      <span>${escapeHtml(n.label || n.path)}</span>
      <span class="size">${fmtBytes(n.bytes)}</span>
    </div>
    <div class="tree-children">${kids}</div>`;
}

function renderDockIndex(state) {
  const items = [];
  const q = state.quick || {};
  const d = state.deep || {};

  if (q.status === "running") {
    items.push({
      title: "Hızlı kategori taraması",
      status: "running",
      detail: `${(q.categories || []).length} kategori · devam ediyor`,
      pct: null,
    });
  } else if (q.status === "done") {
    items.push({
      title: "Hızlı kategori taraması",
      status: "done",
      detail: `${(q.categories || []).length} kategori tamam`,
      pct: 100,
    });
  }

  if (d.status === "running") {
    items.push({
      title: "C:\\ derin tarama (dizin)",
      status: "running",
      detail: d.current || "taranıyor…",
      pct: d.progress || 0,
    });
  } else if (d.status === "done") {
    items.push({
      title: "C:\\ derin tarama (dizin)",
      status: "done",
      detail: `${(d.roots || []).length} kök klasör`,
      pct: 100,
    });
  } else if (d.status === "error") {
    items.push({
      title: "C:\\ derin tarama",
      status: "error",
      detail: d.error || "hata",
      pct: null,
    });
  }

  const w = state.workspaces || {};
  if (w.status === "running") {
    items.push({
      title: "Geliştirme alanları (www)",
      status: "running",
      detail: w.current || "taranıyor…",
      pct: w.progress || 0,
    });
  } else if (w.status === "done" && (w.roots || []).length) {
    items.push({
      title: "Geliştirme alanları (www)",
      status: "done",
      detail: (w.roots || []).map((r) => r.label).join(" · "),
      pct: 100,
    });
  }

  const activeOnly = items.filter((i) => i.status === "running");
  const show = activeOnly.length ? activeOnly : items.slice(0, 2);

  $("#indexEmpty").style.display = show.length ? "none" : "block";
  $("#indexList").innerHTML = show
    .map(
      (i) => `
    <div class="dock-item">
      <div class="t">
        <strong>${escapeHtml(i.title)}</strong>
        <span class="st ${i.status}">${i.status}</span>
      </div>
      <div class="path">${escapeHtml(i.detail || "")}</div>
      ${
        i.pct != null
          ? `<div class="bar"><i style="width:${i.pct}%"></i></div>`
          : i.status === "running"
            ? `<div class="bar"><i style="width:35%"></i></div>`
            : ""
      }
    </div>`
    )
    .join("");

  // Auto-switch to Dizin tab when scan starts
  if (activeOnly.length && !dockUserLocked) {
    setDockTab("index");
  }
}

function renderDockJobs(queue) {
  const jobs = (queue && queue.jobs) || [];
  const active = (queue && queue.active) || 0;
  const badge = $("#jobBadge");
  if (active > 0) {
    badge.textContent = String(active);
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
  }

  const visible = jobs.slice().reverse();
  $("#jobsEmpty").style.display = visible.length ? "none" : "block";
  $("#jobsList").innerHTML = visible
    .map((j) => {
      const cancelBtn =
        j.status === "queued"
          ? `<div class="row-actions"><button type="button" class="btn ghost tiny" data-cancel="${j.id}">Sıradan çıkar</button></div>`
          : "";
      const err = j.error ? `<div class="path">${escapeHtml(j.error)}</div>` : "";
      const sum = j.summary ? `<div class="path">${escapeHtml(j.summary)}</div>` : "";
      return `
      <div class="dock-item">
        <div class="t">
          <strong>${escapeHtml(j.title || j.action)}</strong>
          <span class="st ${j.status}">${j.status}</span>
        </div>
        <div class="path">#${escapeHtml(j.id)} · ${escapeHtml(j.action)}</div>
        ${sum}
        ${err}
        ${cancelBtn}
      </div>`;
    })
    .join("");

  $$("#jobsList [data-cancel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api("/api/queue/cancel", {
        method: "POST",
        body: JSON.stringify({ id: btn.dataset.cancel }),
      });
      await refresh();
    });
  });

  if (active > 0 && !dockUserLocked) {
    setDockTab("jobs");
  }
}

let dockUserLocked = false;
let dockLockTimer = null;

function setDockTab(name) {
  $$(".dock-tab").forEach((t) => t.classList.toggle("active", t.dataset.dock === name));
  $$(".dock-panel").forEach((p) => p.classList.toggle("active", p.id === `dock-${name}`));
}

$$(".dock-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    dockUserLocked = true;
    clearTimeout(dockLockTimer);
    dockLockTimer = setTimeout(() => {
      dockUserLocked = false;
    }, 8000);
    setDockTab(tab.dataset.dock);
  });
});

$("#dockToggle").addEventListener("click", () => {
  $("#dock").classList.toggle("collapsed");
  $("#dockToggle").textContent = $("#dock").classList.contains("collapsed") ? "+" : "−";
});

$("#btnClearJobs").addEventListener("click", async () => {
  await api("/api/queue/clear", { method: "POST", body: "{}" });
  await refresh();
});

function renderDockerGap(data) {
  const el = $("#dockerGap");
  if (!el) return;
  const disk = data?.footprint?.disk_bytes || 0;
  const engine = data?.engine_bytes || data?.total_bytes || 0;
  const daemonUp = !!(data?.available || data?.daemon?.running);
  const gap = data?.gap_bytes != null ? data.gap_bytes : Math.max(0, disk - engine);
  const primary = data?.footprint?.primary_vhdx;
  const found = data?.footprint?.found;

  if (!disk && !engine && !found) {
    el.className = "banner hidden";
    return;
  }

  if (!found || disk === 0) {
    el.className = "banner warn";
    el.innerHTML = `
      <strong>Disk vs Engine</strong>
      <p class="note" style="margin:0.35rem 0 0">
        VHDX dosyası okunamadı. Kategorilerdeki Docker klasör boyutuna bak.
      </p>`;
    return;
  }

  // Docker kapalıyken engine 0 B — bunu "161 GB şişme" diye yutturmayalım
  if (!daemonUp) {
    el.className = "banner warn";
    el.innerHTML = `
      <strong>Docker kapalı — VHDX hâlâ diskte</strong>
      <div style="margin-top:0.35rem;font-size:0.88rem">
        <div><strong>Disk (VHDX):</strong> <span class="size">${fmtBytes(disk)}</span>${primary ? ` · <code>${escapeHtml(primary.label)}</code>` : ""}</div>
        <div><strong>Engine:</strong> ölçülemedi (daemon yok) — 0 B demek “içerisi boş” değil.</div>
      </div>
      <p class="note" style="margin:0.45rem 0 0">
        VHDX küçültme Docker <em>çalışmadan</em> yapılır; hatta kapalıyken daha doğru.
        Önce Docker’ı tamamen kapat, sonra küçült; bitince Docker’ı elle aç.
      </p>
      <div class="actions">
        <button type="button" class="btn danger tiny" data-clean="docker_compact_vhdx">VHDX küçült (Docker kapalı olsun)</button>
      </div>`;
    return;
  }

  const bigGap = gap > 5 * 1024 * 1024 * 1024;
  el.className = "banner " + (bigGap ? "warn" : "ok");
  el.innerHTML = `
    <strong>Disk vs Engine farkı</strong>
    <div style="margin-top:0.35rem;display:grid;gap:0.35rem;font-size:0.88rem">
      <div><strong>Disk (VHDX):</strong> Windows dosya boyutu — <span class="size">${fmtBytes(disk)}</span>${primary ? ` · <code>${escapeHtml(primary.label)}</code>` : ""}</div>
      <div><strong>Engine (df):</strong> Docker içi veri — <span class="size">${fmtBytes(engine)}</span></div>
      <div><strong>Fark:</strong> muhtemel şişme — <span class="size">${fmtBytes(gap)}</span></div>
    </div>
    <p class="note" style="margin:0.45rem 0 0">Prune engine’i temizler; VHDX ayrıca küçültülmeden Windows’ta büyük kalır. Küçültme Docker kapalıyken yapılır.</p>
    ${
      bigGap
        ? `<div class="actions"><button type="button" class="btn danger tiny" data-clean="docker_compact_vhdx">VHDX küçült</button></div>`
        : ""
    }`;
}

let dockerAbort = null;

async function loadDocker() {
  if (dockerAbort) dockerAbort.abort();
  dockerAbort = new AbortController();
  const signal = dockerAbort.signal;
  $("#dockerSummary").textContent = "Docker okunuyor…";
  try {
    const data = await api("/api/docker?fresh=1", { signal });
    if (signal.aborted) return;
    renderDockerWarn(null, data);
    if (!data.available) {
      $("#dockerSummary").textContent = data.error || "Docker kullanılamıyor";
      $("#dockerTypes").innerHTML = "";
      $("#dockerImages").innerHTML = "";
      $("#dockerActions").innerHTML = "";
      renderDockerGap(data);
      return;
    }
    renderDockerGap(data);
    $("#dockerSummary").textContent = `Engine içeriği ~${fmtBytes(data.total_bytes || data.engine_bytes)} (docker system df)${data.daemon?.version ? " · engine " + data.daemon.version : ""}`;
    $("#dockerTypes").innerHTML = (data.types || [])
      .map(
        (t) => `
    <div class="item">
      <div><strong>${escapeHtml(t.type)}</strong><div class="path">reclaimable: ${escapeHtml(String(t.reclaimable || ""))}</div></div>
      <div class="size">${escapeHtml(String(t.size || fmtBytes(t.size_bytes)))}</div>
    </div>`
      )
      .join("");

    const imgs = data.images || [];
    $("#dockerImages").innerHTML = `
    <table>
      <thead><tr><th>Repo</th><th>Tag</th><th>Size</th><th>ID</th></tr></thead>
      <tbody>
        ${imgs
          .slice(0, 80)
          .map(
            (i) => `<tr>
              <td>${escapeHtml(i.repo || "")}</td>
              <td>${escapeHtml(i.tag || "")}</td>
              <td>${escapeHtml(i.size || "")}</td>
              <td>${escapeHtml(i.id || "")}</td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table>`;

    const dockerActs = [
      ["docker_containers", "Container prune"],
      ["docker_dangling_images", "Dangling images"],
      ["docker_unused_images", "Unused images (-a)"],
      ["docker_volumes", "Volume prune"],
      ["docker_build_cache", "Build cache"],
      ["docker_system_prune", "System prune -af"],
      ["docker_compact_vhdx", "VHDX küçült (~disk)"],
    ];
    $("#dockerActions").innerHTML = dockerActs
      .map(
        ([id, label]) =>
          `<button type="button" class="btn ${id === "docker_compact_vhdx" || id === "docker_system_prune" || id === "docker_unused_images" || id === "docker_volumes" ? "danger" : "safe"}" data-clean="${id}">${label}</button>`
      )
      .join("");
  } catch (e) {
    if (e?.name === "AbortError") return;
    $("#dockerSummary").textContent = "Docker okunamadı: " + (e?.message || e);
  }
}

const BUILTIN_ACTIONS = {
  docker_compact_vhdx: {
    title: "Docker VHDX küçült (WSL disk)",
    risk: "medium",
    desc: "wsl --shutdown + docker_data.vhdx compact. Docker Desktop kapanır; sonra elle aç.",
  },
  docker_system_prune: {
    title: "Docker system prune (agresif)",
    risk: "medium",
    desc: "docker system prune -af --volumes",
  },
};

let actionMeta = { ...BUILTIN_ACTIONS };
let confirmBusy = false;

async function loadActions() {
  const data = await api("/api/actions");
  const actions = data.actions || [];
  actionMeta = { ...BUILTIN_ACTIONS, ...Object.fromEntries(actions.map((a) => [a.id, a])) };
  renderAdmin(!!data.admin);
  $("#actionList").innerHTML = actions
    .map(
      (a) => `
    <div class="action">
      <div class="meta">
        <strong>${escapeHtml(a.title)} <span class="badge ${a.risk}">${a.risk}</span></strong>
        <small>${escapeHtml(a.desc)}</small>
      </div>
      <button type="button" class="btn ${a.risk === "medium" ? "danger" : "safe"}" data-clean="${a.id}">Sıraya al</button>
    </div>`
    )
    .join("");
}

function confirmClean(actionId) {
  if (!actionId || confirmBusy) return;
  const meta =
    actionMeta[actionId] ||
    BUILTIN_ACTIONS[actionId] || {
      title: actionId,
      desc: actionId,
      risk: "medium",
    };
  $("#dlgTitle").textContent = meta.title || actionId;
  $("#dlgBody").textContent =
    (meta.desc || "") +
    "\n\nİşlem sıraya alınacak; aynı anda birden fazla çalışmaz." +
    (meta.risk === "medium" ? "\nOrta risk — emin misin?" : "");

  const dlg = $("#confirmDlg");
  const okBtn = $("#dlgOk");
  const cancelBtn = $("#dlgCancel");

  const cleanup = () => {
    okBtn.onclick = null;
    cancelBtn.onclick = null;
  };

  cancelBtn.onclick = () => {
    cleanup();
    dlg.close();
  };

  okBtn.onclick = async () => {
    if (confirmBusy) return;
    confirmBusy = true;
    okBtn.disabled = true;
    try {
      const res = await api("/api/clean", {
        method: "POST",
        body: JSON.stringify({ action: actionId, confirm: true }),
      });
      cleanup();
      dlg.close();
      const log = $("#cleanLog");
      if (res.queued) {
        const msg =
          `Sıraya alındı: ${meta.title || actionId}` +
          (res.duplicate ? " (zaten sırada)" : ` (#${res.job?.id || "?"})`);
        if (log) log.textContent = msg;
        const gap = $("#dockerGap");
        if (gap && !gap.classList.contains("hidden")) {
          const note = document.createElement("p");
          note.className = "note";
          note.style.marginTop = "0.5rem";
          note.textContent = msg + " — sağ alt İşlemler sekmesine bak.";
          gap.appendChild(note);
        }
        setDockTab("jobs");
        dockUserLocked = true;
        clearTimeout(dockLockTimer);
        dockLockTimer = setTimeout(() => {
          dockUserLocked = false;
        }, 8000);
        await refresh();
      } else {
        const err = res.error || JSON.stringify(res);
        if (log) log.textContent = "Sıraya alınamadı: " + err;
        alert("Sıraya alınamadı: " + err);
      }
    } catch (e) {
      cleanup();
      dlg.close();
      alert("İstek hatası: " + (e?.message || e));
    } finally {
      confirmBusy = false;
      okBtn.disabled = false;
    }
  };

  if (!dlg.open) dlg.showModal();
}

// Tek bağlama: yeniden çizilen butonlar da çalışır
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-clean]");
  if (!btn) return;
  e.preventDefault();
  confirmClean(btn.dataset.clean);
});

async function refresh() {
  const state = await api("/api/status");
  renderDisk(state.disk);
  renderAdmin(!!state.admin);
  renderCategories(state);
  renderDeep(state);
  renderWorkspaces(state);
  renderDockIndex(state);
  renderDockJobs(state.queue);
  return state;
}

const TAB_KEY = "pcYerAcma.activeTab";

function activateTab(name, { persist = true } = {}) {
  const tab = $(`.tab[data-tab="${name}"]`);
  const panel = $(`#tab-${name}`);
  if (!tab || !panel) return false;
  $$(".tab").forEach((t) => t.classList.remove("active"));
  $$(".panel").forEach((p) => p.classList.remove("active"));
  tab.classList.add("active");
  panel.classList.add("active");
  if (persist) {
    try {
      localStorage.setItem(TAB_KEY, name);
    } catch (_) {}
  }
  if (name === "docker") loadDocker();
  return true;
}

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});

function restoreTab() {
  let saved = "cats";
  try {
    saved = localStorage.getItem(TAB_KEY) || "cats";
  } catch (_) {}
  if (!activateTab(saved, { persist: false })) {
    activateTab("cats", { persist: false });
  }
}

$("#btnQuick").addEventListener("click", async () => {
  $("#btnQuick").disabled = true;
  await api("/api/scan/quick");
  setDockTab("index");
  $("#btnQuick").disabled = false;
});

$("#btnDeep").addEventListener("click", async () => {
  $("#btnDeep").disabled = true;
  await api("/api/scan/deep");
  setDockTab("index");
  setTimeout(() => ($("#btnDeep").disabled = false), 1500);
});

$("#btnProjects").addEventListener("click", async () => {
  $("#btnProjects").disabled = true;
  await api("/api/scan/workspaces");
  setDockTab("index");
  setTimeout(() => ($("#btnProjects").disabled = false), 1500);
});

$("#btnDocker").addEventListener("click", loadDocker);

loadActions();
restoreTab();
refresh();
setInterval(refresh, 1500);
