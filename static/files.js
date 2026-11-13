// Dosyalar sekmesi: büyük, gereksiz olabilecek ve kopya dosyalar.
// app.js'deki $, $$, api, fmtBytes, escapeHtml, fmtAge yardımcılarını kullanır.
(() => {
  const ROOTS_KEY = "pcYerAcma.files.roots";
  const EXTRA_KEY = "pcYerAcma.files.extra";
  const PAGE = 150;

  const st = {
    data: null,
    status: null,
    sub: "fdups",
    filter: "",
    limit: PAGE,
    selected: new Map(), // path -> size
    lastFinished: null,
    roots: [],
  };

  const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleDateString("tr-TR") : "—");
  const lsGet = (k, d) => {
    try {
      const v = localStorage.getItem(k);
      return v == null ? d : JSON.parse(v);
    } catch (_) {
      return d;
    }
  };
  const lsSet = (k, v) => {
    try {
      localStorage.setItem(k, JSON.stringify(v));
    } catch (_) {}
  };

  // ------------------------------------------------------------ klasörler
  async function loadRoots() {
    const res = await api("/api/files/roots");
    st.roots = res.roots || [];
    const saved = lsGet(ROOTS_KEY, null);
    $("#filesRoots").innerHTML = st.roots
      .map((r, i) => {
        const on = r.exists && (saved ? saved.includes(r.path) : r.default);
        return `<label class="${r.exists ? "" : "missing"}">
          <input type="checkbox" data-root="${i}" ${on ? "checked" : ""} ${r.exists ? "" : "disabled"} />
          <span>${escapeHtml(r.label)} <small>${escapeHtml(r.path)}</small></span>
        </label>`;
      })
      .join("");
    $("#filesExtra").value = lsGet(EXTRA_KEY, "");
    rootsSummary();
  }

  function chosenRoots() {
    const out = [];
    $$("#filesRoots input[data-root]").forEach((cb) => {
      if (cb.checked) out.push(st.roots[Number(cb.dataset.root)]);
    });
    $("#filesExtra")
      .value.split(/\r?\n/)
      .map((s) => s.trim().replace(/^"|"$/g, ""))
      .filter(Boolean)
      .forEach((p) => out.push({ label: p.split(/[\\/]/).filter(Boolean).pop() || p, path: p }));
    return out;
  }

  function rootsSummary() {
    const r = chosenRoots();
    $("#filesRootsSummary").textContent = `· ${r.map((x) => x.label).join(", ") || "seçilmedi"}`;
  }

  $("#filesRoots").addEventListener("change", () => {
    lsSet(
      ROOTS_KEY,
      $$("#filesRoots input[data-root]")
        .filter((cb) => cb.checked)
        .map((cb) => st.roots[Number(cb.dataset.root)].path)
    );
    rootsSummary();
  });
  $("#filesExtra").addEventListener("input", () => {
    lsSet(EXTRA_KEY, $("#filesExtra").value);
    rootsSummary();
  });

  // ------------------------------------------------------------ durum
  function renderStatus(s) {
    const running = s.status === "running";
    $("#filesBar").style.width = `${running ? s.progress || 0 : s.status === "done" ? 100 : 0}%`;
    $("#btnFilesScan").disabled = running;
    $("#btnFilesCancel").classList.toggle("hidden", !running);
    const age = s.finished ? fmtAge(Date.now() / 1000 - s.finished) : null;
    $("#filesStatus").textContent = running
      ? `${s.phase || "Taranıyor"} · %${s.progress || 0} · ${s.current || ""}`
      : s.status === "done"
        ? `Son tarama ${age || ""}${st.data?.stats?.seconds ? ` · ${st.data.stats.seconds} sn sürdü` : ""}`
        : s.status === "cancelled"
          ? "Tarama durduruldu"
          : s.status === "error"
            ? `Hata: ${s.error}`
            : "Henüz taranmadı. Klasörleri seçip Tara'ya bas.";
  }

  function renderStats() {
    const s = st.data?.stats;
    if (!s || !s.files) {
      $("#filesStats").innerHTML = "";
      return;
    }
    const tile = (v, t) => `<div class="stat"><b>${v}</b><span>${t}</span></div>`;
    $("#filesStats").innerHTML = [
      tile(fmtBytes(s.folder_dup_wasted || 0), `kopya klasörlerde boşa · ${s.folder_dup_groups || 0} grup`),
      tile(fmtBytes(s.dup_wasted), `kopya dosyalarda boşa · ${s.dup_groups} grup`),
      tile(fmtBytes(s.junk_bytes), `gereksiz olabilir · ${s.junk_count} dosya`),
      tile(fmtBytes(s.big_bytes), `100 MB üstü · ${s.big_count} dosya`),
      tile(s.files.toLocaleString("tr-TR"), `dosya tarandı · ${fmtBytes(s.bytes)}${s.cloud_skipped ? ` · ${s.cloud_skipped} bulutta atlandı` : ""}`),
    ].join("");
    $("#cntFdups").textContent = `(${(st.data.folder_dups || []).length})`;
    $("#cntDups").textContent = `(${(st.data.duplicates || []).length})`;
    $("#cntBig").textContent = `(${(st.data.big || []).length})`;
    $("#cntJunk").textContent = `(${(st.data.junk || []).length})`;
  }

  // ------------------------------------------------------------ listeler
  const match = (r) => {
    if (!st.filter) return true;
    const q = st.filter.toLocaleLowerCase("tr-TR");
    return (r.name + " " + r.dir).toLocaleLowerCase("tr-TR").includes(q);
  };

  function fileRow(r, { keep = false, reasons = null } = {}) {
    const sel = st.selected.has(r.path);
    return `<div class="frow${sel ? " sel" : ""}">
      <input type="checkbox" data-path="${escapeHtml(r.path)}" data-size="${r.size}" ${sel ? "checked" : ""} />
      <div>
        <div class="fname">${r.is_dir ? "📁 " : ""}${escapeHtml(r.name)}${keep ? '<span class="tag keep">tut</span>' : ""}${r.root ? `<span class="tag">${escapeHtml(r.root)}</span>` : ""}</div>
        <div class="fdir">${escapeHtml(r.dir)}</div>
        ${reasons ? `<div class="reasons">${reasons.map(escapeHtml).join(" · ")}</div>` : ""}
      </div>
      <div class="fmeta"><span class="size">${fmtBytes(r.size)}</span><br />${fmtDate(r.mtime)}</div>
      <button type="button" class="btn ghost tiny" data-reveal="${escapeHtml(r.path)}">Göster</button>
    </div>`;
  }

  function renderList() {
    const d = st.data || {};
    let html = "";
    let total = 0;
    if (st.sub === "fdups") {
      const groups = (d.folder_dups || []).filter((g) => g.folders.some(match));
      total = groups.length;
      html = groups
        .slice(0, st.limit)
        .map(
          (g) => `<div class="dgroup">
          <div class="dgroup-head">
            <span>${g.count} aynı klasör · her biri ${fmtBytes(g.size)}, ${g.files.toLocaleString("tr-TR")} dosya</span>
            <span class="size">boşa ${fmtBytes(g.wasted)}</span>
          </div>
          ${g.folders.map((f) => fileRow(f, { keep: f.keep })).join("")}
        </div>`
        )
        .join("");
      if (!total) html = `<p class="hint">Kopya klasör bulunmadı.</p>`;
    } else if (st.sub === "dups") {
      const groups = (d.duplicates || []).filter((g) => g.files.some(match));
      total = groups.length;
      html = groups
        .slice(0, st.limit)
        .map(
          (g) => `<div class="dgroup">
          <div class="dgroup-head">
            <span>${g.count} aynı dosya · her biri ${fmtBytes(g.size)}${g.different_names ? " · adları farklı" : ""}</span>
            <span class="size">boşa ${fmtBytes(g.wasted)}</span>
          </div>
          ${g.files.map((f) => fileRow(f, { keep: f.keep })).join("")}
        </div>`
        )
        .join("");
      if (!total) html = `<p class="hint">Kopya dosya bulunmadı.</p>`;
    } else {
      const rows = (st.sub === "big" ? d.big : d.junk) || [];
      const list = rows.filter(match);
      total = list.length;
      html = list
        .slice(0, st.limit)
        .map((r) => fileRow(r, { reasons: st.sub === "junk" ? r.reasons : null }))
        .join("");
      if (!total) html = `<p class="hint">Liste boş.</p>`;
    }
    if (total > st.limit) {
      html += `<div class="more"><button type="button" class="btn ghost tiny" id="filesMore">${total - st.limit} tane daha göster</button></div>`;
    }
    $("#filesList").innerHTML = st.data ? html : "";
    $("#btnSelSuggested").classList.toggle("hidden", st.sub === "big");
    renderSelBar();
  }

  function renderSelBar() {
    const n = st.selected.size;
    let bytes = 0;
    st.selected.forEach((s) => (bytes += s));
    $("#filesSelBar").classList.toggle("hidden", n === 0);
    $("#filesSelText").textContent = `${n} öğe seçili · ${fmtBytes(bytes)} yer açılır`;
  }

  // Önerilen: kopyalarda "tut" dışındakiler; gereksizlerde tartışmasız nedenler
  const SURE = ["Yarım kalmış", "Kurulum dosyası", "Arşiv zaten açılmış"];
  function selectSuggested() {
    const d = st.data || {};
    if (st.sub === "fdups") {
      (d.folder_dups || []).forEach((g) => {
        if (!g.folders.some(match)) return;
        g.folders.forEach((f) => !f.keep && st.selected.set(f.path, f.size));
      });
    } else if (st.sub === "dups") {
      (d.duplicates || []).forEach((g) => {
        if (!g.files.some(match)) return;
        g.files.forEach((f) => !f.keep && st.selected.set(f.path, f.size));
      });
    } else if (st.sub === "junk") {
      (d.junk || []).filter(match).forEach((r) => {
        if (r.reasons.some((x) => SURE.some((s) => x.startsWith(s)))) st.selected.set(r.path, r.size);
      });
    }
    renderList();
  }

  // ------------------------------------------------------------ olaylar
  $("#filesList").addEventListener("change", (e) => {
    const cb = e.target.closest("input[data-path]");
    if (!cb) return;
    if (cb.checked) st.selected.set(cb.dataset.path, Number(cb.dataset.size));
    else st.selected.delete(cb.dataset.path);
    cb.closest(".frow").classList.toggle("sel", cb.checked);
    renderSelBar();
  });

  $("#filesList").addEventListener("click", (e) => {
    const rev = e.target.closest("[data-reveal]");
    if (rev) {
      api("/api/files/reveal", { method: "POST", body: JSON.stringify({ path: rev.dataset.reveal }) });
      return;
    }
    if (e.target.id === "filesMore") {
      st.limit += PAGE;
      renderList();
    }
  });

  $$("#filesSubtabs .subtab").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#filesSubtabs .subtab").forEach((x) => x.classList.toggle("active", x === b));
      st.sub = b.dataset.sub;
      st.limit = PAGE;
      renderList();
    })
  );

  let filterTimer;
  $("#filesFilter").addEventListener("input", (e) => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      st.filter = e.target.value.trim();
      st.limit = PAGE;
      renderList();
    }, 150);
  });

  $("#btnSelSuggested").addEventListener("click", selectSuggested);
  $("#btnSelNone").addEventListener("click", () => {
    st.selected.clear();
    renderList();
  });

  $("#btnFilesScan").addEventListener("click", async () => {
    const roots = chosenRoots();
    if (!roots.length) {
      $("#filesStatus").textContent = "En az bir klasör seç.";
      $("#filesRootsBox").open = true;
      return;
    }
    $("#btnFilesScan").disabled = true;
    const res = await api("/api/files/scan", { method: "POST", body: JSON.stringify({ roots }) });
    if (!res.ok) $("#filesStatus").textContent = res.error || "Başlatılamadı";
    poll();
  });

  $("#btnFilesCancel").addEventListener("click", () =>
    api("/api/files/cancel", { method: "POST", body: "{}" }).then(poll)
  );

  // Geri dönüşüm kutusu
  const dlg = $("#trashDlg");
  $("#btnFilesTrash").addEventListener("click", () => {
    const paths = [...st.selected.keys()];
    let bytes = 0;
    st.selected.forEach((s) => (bytes += s));
    $("#trashBody").textContent = `${paths.length} öğe (${fmtBytes(bytes)}) Geri Dönüşüm Kutusu'na taşınacak. Oradan geri alınabilir; kutu boşaltılınca yer açılır.`;
    $("#trashList").innerHTML = paths
      .slice(0, 300)
      .map((p) => `<li>${escapeHtml(p)}</li>`)
      .join("") + (paths.length > 300 ? `<li>… ve ${paths.length - 300} dosya daha</li>` : "");
    $("#trashOk").disabled = false;
    dlg.showModal();
  });
  $("#trashCancel").addEventListener("click", () => dlg.close());
  $("#trashOk").addEventListener("click", async () => {
    $("#trashOk").disabled = true;
    const paths = [...st.selected.keys()];
    const res = await api("/api/files/trash", {
      method: "POST",
      body: JSON.stringify({ paths, confirm: true }),
    });
    (res.trashed || []).forEach((p) => st.selected.delete(p));
    if (res.failed?.length) {
      $("#trashBody").textContent = `${res.trashed.length} dosya taşındı, ${res.failed.length} dosya taşınamadı:`;
      $("#trashList").innerHTML = res.failed
        .map((f) => `<li>${escapeHtml(f.path)} · ${escapeHtml(f.error)}</li>`)
        .join("");
    } else {
      dlg.close();
    }
    await loadFull();
  });

  // ------------------------------------------------------------ yükleme
  async function loadFull() {
    st.data = await api("/api/files");
    // Artık listede olmayan seçimleri bırak
    const alive = new Set();
    (st.data.big || []).forEach((r) => alive.add(r.path));
    (st.data.junk || []).forEach((r) => alive.add(r.path));
    (st.data.duplicates || []).forEach((g) => g.files.forEach((f) => alive.add(f.path)));
    (st.data.folder_dups || []).forEach((g) => g.folders.forEach((f) => alive.add(f.path)));
    [...st.selected.keys()].forEach((p) => !alive.has(p) && st.selected.delete(p));
    renderStats();
    renderStatus(st.data);
    renderList();
  }

  let pollTimer;
  async function poll() {
    clearTimeout(pollTimer);
    const s = await api("/api/files/status");
    renderStatus(s);
    const visible = $("#tab-files").classList.contains("active");
    if (s.status !== "running" && s.finished !== st.lastFinished) {
      st.lastFinished = s.finished;
      await loadFull();
    }
    pollTimer = setTimeout(poll, s.status === "running" ? 1000 : visible ? 4000 : 10000);
  }

  loadRoots().then(poll);
})();
