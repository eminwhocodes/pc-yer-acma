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
    keepOverride: new Map(), // grup -> tutulacak yol
    order: [], // ekrandaki satır sırası (Shift aralığı için)
    lastIdx: null,
    trashJob: null,
    lastRev: null,
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

  // Görünmez yön karakterlerini (ör. masaüstündeki adsız klasör) okunur göster
  const INVIS = /[\u200b-\u200f\u202a-\u202e\u2066-\u2069]+/g;
  const disp = (s) => escapeHtml(String(s)).replace(INVIS, '<span class="invis">‹görünmez›</span>');

  // Kopya grupları: hangi üye tutulacak (kullanıcı "Bunu tut" ile değiştirebilir)
  const gkey = (kind, g) => `${kind}:${g.hash}`;
  const membersOf = (kind, g) => (kind === "fdups" ? g.folders : g.files);
  function keepPath(kind, g) {
    const ms = membersOf(kind, g);
    const o = st.keepOverride.get(gkey(kind, g));
    if (o && ms.some((m) => m.path === o)) return o;
    return (ms.find((m) => m.keep) || ms[0]).path;
  }

  function fileRow(r, { idx, keep = false, reasons = null, group = null } = {}) {
    const sel = st.selected.has(r.path);
    const keepBtn =
      group && !keep
        ? `<button type="button" class="btn ghost tiny" data-keep="${escapeHtml(group)}" data-path="${escapeHtml(r.path)}" title="Bu kopya kalsın, diğerleri silinsin">Bunu tut</button>`
        : "";
    return `<div class="frow${sel ? " sel" : ""}${keep ? " keeprow" : ""}" data-idx="${idx}">
      <input type="checkbox" data-path="${escapeHtml(r.path)}" data-size="${r.size}" ${sel ? "checked" : ""} ${keep ? 'disabled title="Tutulacak kopya"' : ""} />
      <div class="fmain">
        <div class="fname">${r.is_dir ? "📁 " : ""}${disp(r.name)}${keep ? '<span class="tag keep">tutulacak</span>' : ""}${r.root ? `<span class="tag">${escapeHtml(r.root)}</span>` : ""}</div>
        <div class="fdir">${disp(r.dir)}</div>
        ${reasons ? `<div class="reasons">${reasons.map(escapeHtml).join(" · ")}</div>` : ""}
      </div>
      <div class="fmeta"><span class="size">${fmtBytes(r.size)}</span><br />${fmtDate(r.mtime)}</div>
      <div class="fact">${keepBtn}<button type="button" class="btn ghost tiny" data-reveal="${escapeHtml(r.path)}">Göster</button></div>
    </div>`;
  }

  function groupBlock(kind, g, headText) {
    const key = gkey(kind, g);
    const kp = keepPath(kind, g);
    const extras = membersOf(kind, g).filter((m) => m.path !== kp);
    const nSel = extras.filter((m) => st.selected.has(m.path)).length;
    const state = nSel === 0 ? "" : nSel === extras.length ? "checked" : "data-mixed";
    const rows = membersOf(kind, g)
      .map((m) => {
        const idx = st.order.push(m.path) - 1;
        return fileRow(m, { idx, keep: m.path === kp, group: key });
      })
      .join("");
    return `<div class="dgroup${nSel ? " has-sel" : ""}">
      <label class="dgroup-head">
        <input type="checkbox" data-group="${escapeHtml(key)}" ${state === "checked" ? "checked" : ""} ${state} />
        <span class="gtext"><strong>Fazlalıkları seç (${extras.length})</strong> · ${headText}</span>
        <span class="size">boşa ${fmtBytes(g.size * extras.length)}</span>
      </label>
      ${rows}
    </div>`;
  }

  function renderList() {
    const d = st.data || {};
    let html = "";
    let total = 0;
    st.order = [];
    if (st.sub === "fdups" || st.sub === "dups") {
      const kind = st.sub;
      const groups = ((kind === "fdups" ? d.folder_dups : d.duplicates) || []).filter((g) =>
        membersOf(kind, g).some(match)
      );
      total = groups.length;
      html = groups
        .slice(0, st.limit)
        .map((g) =>
          groupBlock(
            kind,
            g,
            kind === "fdups"
              ? `${g.count} aynı klasör, her biri ${fmtBytes(g.size)} · ${g.files.toLocaleString("tr-TR")} dosya`
              : `${g.count} aynı dosya, her biri ${fmtBytes(g.size)}${g.different_names ? " · adları farklı" : ""}`
          )
        )
        .join("");
      if (!total) html = `<p class="hint">${kind === "fdups" ? "Kopya klasör" : "Kopya dosya"} bulunmadı.</p>`;
    } else {
      const rows = (st.sub === "big" ? d.big : d.junk) || [];
      const list = rows.filter(match);
      total = list.length;
      html = list
        .slice(0, st.limit)
        .map((r) => fileRow(r, { idx: st.order.push(r.path) - 1, reasons: st.sub === "junk" ? r.reasons : null }))
        .join("");
      if (!total) html = `<p class="hint">Liste boş.</p>`;
    }
    if (total > st.limit) {
      html += `<div class="more"><button type="button" class="btn ghost tiny" id="filesMore">${total - st.limit} tane daha göster</button></div>`;
    }
    $("#filesList").innerHTML = st.data ? html : "";
    $$("#filesList input[data-mixed]").forEach((cb) => (cb.indeterminate = true));
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

  function findGroup(key) {
    const [kind, hash] = key.split(":");
    const list = (kind === "fdups" ? st.data?.folder_dups : st.data?.duplicates) || [];
    const g = list.find((x) => x.hash === hash);
    return g ? { kind, g } : null;
  }

  function sizeOf(path) {
    const cb = $(`#filesList input[data-path="${CSS.escape(path)}"]`);
    return cb ? Number(cb.dataset.size) : 0;
  }

  // Önerilen: kopyalarda "tutulacak" dışındakiler; gereksizlerde tartışmasız nedenler
  const SURE = ["Yarım kalmış", "Kurulum dosyası", "Arşiv zaten açılmış"];
  function selectSuggested() {
    const d = st.data || {};
    if (st.sub === "fdups" || st.sub === "dups") {
      const kind = st.sub;
      ((kind === "fdups" ? d.folder_dups : d.duplicates) || []).forEach((g) => {
        if (!membersOf(kind, g).some(match)) return;
        const kp = keepPath(kind, g);
        membersOf(kind, g).forEach((m) => m.path !== kp && st.selected.set(m.path, m.size));
      });
    } else if (st.sub === "junk") {
      (d.junk || []).filter(match).forEach((r) => {
        if (r.reasons.some((x) => SURE.some((s) => x.startsWith(s)))) st.selected.set(r.path, r.size);
      });
    }
    renderList();
  }

  // ------------------------------------------------------------ olaylar
  function setRow(path, on) {
    const cb = $(`#filesList input[data-path="${CSS.escape(path)}"]`);
    if (!cb || cb.disabled) return;
    if (on) st.selected.set(path, Number(cb.dataset.size));
    else st.selected.delete(path);
  }

  // Satıra tıklayınca seç; Shift ile son tıklanandan buraya kadar aralık
  $("#filesList").addEventListener("click", (e) => {
    const rev = e.target.closest("[data-reveal]");
    if (rev) {
      api("/api/files/reveal", { method: "POST", body: JSON.stringify({ path: rev.dataset.reveal }) });
      return;
    }
    const keep = e.target.closest("[data-keep]");
    if (keep) {
      const fg = findGroup(keep.dataset.keep);
      const newKeep = keep.dataset.path;
      if (fg) {
        const oldKeep = keepPath(fg.kind, fg.g);
        // Grubun geri kalanının hepsi seçiliyse eski "tutulacak" da fazlalık olarak seçilsin
        const others = membersOf(fg.kind, fg.g).filter((m) => m.path !== oldKeep && m.path !== newKeep);
        const wasAll = others.every((m) => st.selected.has(m.path));
        st.keepOverride.set(keep.dataset.keep, newKeep);
        const old = membersOf(fg.kind, fg.g).find((m) => m.path === oldKeep);
        if (wasAll && old) st.selected.set(old.path, old.size);
      }
      st.selected.delete(newKeep);
      renderList();
      return;
    }
    if (e.target.id === "filesMore") {
      st.limit += PAGE;
      renderList();
      return;
    }
    const head = e.target.closest("input[data-group]");
    if (head) {
      const fg = findGroup(head.dataset.group);
      if (fg) {
        const kp = keepPath(fg.kind, fg.g);
        const extras = membersOf(fg.kind, fg.g).filter((m) => m.path !== kp);
        const allOn = extras.every((m) => st.selected.has(m.path));
        extras.forEach((m) => (allOn ? st.selected.delete(m.path) : st.selected.set(m.path, m.size)));
      }
      renderList();
      return;
    }
    if (e.target.closest(".dgroup-head")) return; // label tıklaması kutuya iletilir
    const row = e.target.closest(".frow");
    if (!row) return;
    const cb = row.querySelector("input[data-path]");
    if (!cb || cb.disabled) return;
    const idx = Number(row.dataset.idx);
    const on = e.target === cb ? cb.checked : !st.selected.has(cb.dataset.path);
    if (e.shiftKey && st.lastIdx != null) {
      const [a, b] = idx < st.lastIdx ? [idx, st.lastIdx] : [st.lastIdx, idx];
      for (let i = a; i <= b; i++) setRow(st.order[i], on);
      window.getSelection()?.removeAllRanges();
    } else {
      setRow(cb.dataset.path, on);
    }
    st.lastIdx = idx;
    renderList();
  });

  $$("#filesSubtabs .subtab").forEach((b) =>
    b.addEventListener("click", () => {
      $$("#filesSubtabs .subtab").forEach((x) => x.classList.toggle("active", x === b));
      st.sub = b.dataset.sub;
      st.limit = PAGE;
      st.lastIdx = null;
      renderList();
    })
  );

  let filterTimer;
  $("#filesFilter").addEventListener("input", (e) => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      st.filter = e.target.value.trim();
      st.limit = PAGE;
      st.lastIdx = null;
      renderList();
    }, 150);
  });

  $("#btnSelSuggested").addEventListener("click", selectSuggested);
  $("#btnSelNone").addEventListener("click", () => {
    st.selected.clear();
    st.lastIdx = null;
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

  // ------------------------------------------------------------ geri dönüşüm kutusu (arka planda)
  function notice(html, kind = "") {
    const el = $("#filesNotice");
    if (!html) {
      el.className = "banner hidden";
      el.innerHTML = "";
      return;
    }
    el.className = `banner ${kind}`;
    el.innerHTML = html;
  }

  function showJobsDock() {
    $("#dock")?.classList.remove("collapsed");
    if (typeof setDockTab === "function") setDockTab("jobs");
  }

  const dlg = $("#trashDlg");
  $("#btnFilesTrash").addEventListener("click", () => {
    const paths = [...st.selected.keys()];
    let bytes = 0;
    st.selected.forEach((s) => (bytes += s));
    $("#trashBody").textContent = `${paths.length} öğe (${fmtBytes(bytes)}) Geri Dönüşüm Kutusu'na taşınacak. İş arka planda sürer, ilerlemesi sağ alttaki İşlemler kutusunda görünür. Oradan geri alınabilir; yer, kutu boşaltılınca açılır.`;
    $("#trashList").innerHTML =
      paths
        .slice(0, 300)
        .map((p) => `<li>${disp(p)}</li>`)
        .join("") + (paths.length > 300 ? `<li>… ve ${paths.length - 300} öğe daha</li>` : "");
    $("#trashOk").disabled = false;
    dlg.showModal();
  });
  $("#trashCancel").addEventListener("click", () => dlg.close());
  $("#trashOk").addEventListener("click", async () => {
    $("#trashOk").disabled = true;
    const paths = [...st.selected.keys()];
    let res;
    try {
      res = await api("/api/files/trash", { method: "POST", body: JSON.stringify({ paths, confirm: true }) });
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    dlg.close();
    const rej = res.rejected || [];
    const rejHtml = rej.length
      ? `<details><summary>${rej.length} öğe taşınmayacak</summary><ul class="trash-list">${rej
          .map((r) => `<li>${disp(r.path)} · ${escapeHtml(r.error)}</li>`)
          .join("")}</ul></details>`
      : "";
    if (!res.ok) {
      notice(`<strong>Taşıma başlamadı:</strong> ${escapeHtml(res.error || "bilinmeyen hata")}${rejHtml}`, "warn");
      return;
    }
    paths.forEach((p) => st.selected.delete(p));
    st.trashJob = res.job.id;
    notice(
      `<strong>${res.count} öğe (${fmtBytes(res.bytes)}) Geri Dönüşüm Kutusu'na taşınıyor.</strong> İlerleme sağ altta, İşlemler'de; liste taşındıkça güncellenir.${rejHtml}`
    );
    showJobsDock();
    renderList();
    poll();
  });

  // ------------------------------------------------------------ yükleme
  async function loadFull() {
    st.data = await api("/api/files");
    st.lastRev = st.data.rev;
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

  async function checkTrashJob() {
    if (!st.trashJob) return false;
    const job = await api(`/api/queue/${st.trashJob}`).catch(() => null);
    if (!job || job.ok === false) {
      st.trashJob = null;
      return false;
    }
    if (job.status === "queued" || job.status === "running") return true;
    st.trashJob = null;
    const ok = job.status === "done";
    notice(
      `<strong>${ok ? "Taşıma bitti" : job.status === "cancelled" ? "Taşıma durduruldu" : "Taşıma hatayla bitti"}:</strong> ${escapeHtml(
        job.detail || job.summary || ""
      )}${job.error ? `<div class="path">${escapeHtml(job.error)}</div>` : ""}`,
      ok ? "ok" : "warn"
    );
    await loadFull();
    return false;
  }

  let pollTimer;
  async function poll() {
    clearTimeout(pollTimer);
    let s;
    try {
      s = await api("/api/files/status");
    } catch (_) {
      pollTimer = setTimeout(poll, 5000);
      return;
    }
    renderStatus(s);
    const visible = $("#tab-files").classList.contains("active");
    if (s.status !== "running" && (s.finished !== st.lastFinished || s.rev !== st.lastRev)) {
      st.lastFinished = s.finished;
      await loadFull();
    }
    const trashing = await checkTrashJob();
    pollTimer = setTimeout(poll, s.status === "running" || trashing ? 1200 : visible ? 4000 : 10000);
  }

  loadRoots().then(poll);
})();
