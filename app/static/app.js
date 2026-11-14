// Live calculation on workflow forms (same behaviour as POC 1).
(function () {
  const form = document.getElementById("wf");
  if (!form || !document.getElementById("derived")) return;
  const $ = (id) => document.getElementById(id);
  const money = (n) => Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const val = (n) => (form.elements[n] ? form.elements[n].value : "");
  let last = null;                  // latest calculation (term in years) for "Keep current ARR"
  async function derive() {
    const body = { start_date: val("start_date"), end_date: val("end_date"), tcv: val("tcv"), term_years: val("term_years") };
    try {
      const d = await (await fetch("/api/derive", { method: "POST", headers: { "Content-Type": "application/json" },
                                                   body: JSON.stringify(body) })).json();
      $("d-error").textContent = d.ok ? "" : d.error;
      if (!d.ok) return;
      last = d;
      const [y, m, dd] = d.renewal_date.split("-");
      $("d-term").textContent = d.term_years;
      $("d-arr").textContent = money(d.arr);
      $("d-monthly").textContent = money(d.monthly);
      $("d-renewal").textContent = `${m}/${dd}/${y}`;
      $("d-years").textContent = d.years.join(", ");
      const ch = $("d-change");
      if (ch) {
        const prior = Number(ch.dataset.prior || 0), diff = d.arr - prior;
        ch.textContent = (diff >= 0 ? "+" : "") + money(diff) + (prior ? ` (${(diff / prior * 100).toFixed(1)}%)` : "");
        ch.style.color = diff < 0 ? "var(--err)" : "var(--ok)";
      }
    } catch (e) { /* ignore */ }
  }
  function prior() {            // renewal: ARR replaced = renewed term + ticked terms merged into it
    const ch = $("d-change");
    if (!ch || ch.dataset.base === undefined) return;
    let p = Number(ch.dataset.base || 0);
    const picked = [...form.querySelectorAll("input[name=rollup]:checked")];
    picked.forEach((x) => { p += Number(x.dataset.arr || 0); });
    ch.dataset.prior = p;
    if ($("d-prior")) $("d-prior").textContent = money(p);
    if ($("merge-total")) $("merge-total").textContent = money(p);
    if ($("merge-text")) {
      $("merge-summary").firstElementChild.textContent = picked.length ? "Merging into this renewal:" : "Not merging other contracts.";
      $("merge-text").textContent = picked.length
        ? picked.map((x) => x.dataset.label).join(", ") + " · ARR being replaced " + money(p) : "";
    }
  }
  const dlg = $("merge-dlg");       // merge popup: opens on load, ticked by default
  if (dlg && dlg.showModal) {
    $("merge-ok").addEventListener("click", () => dlg.close());
    $("merge-change").addEventListener("click", () => dlg.showModal());
    $("merge-none").addEventListener("click", () => {
      form.querySelectorAll("input[name=rollup]").forEach((x) => { x.checked = false; });
      prior(); derive(); dlg.close();
    });
    if (dlg.dataset.autoopen === "1") dlg.showModal();
  }
  const keep = $("keep-arr");       // amendment: same ARR over the new period (only the dates change)
  if (keep) keep.addEventListener("click", async () => {
    await derive();
    if (!last) return;
    const tcv = Math.round(Number(keep.dataset.arr) * last.months / 12 * 100) / 100;
    form.elements.tcv.value = tcv.toFixed(2);
    derive();
  });
  form.querySelectorAll(".drv").forEach((el) => el.addEventListener("input", derive));
  form.querySelectorAll("input[name=rollup]").forEach((el) => el.addEventListener("change", () => { prior(); derive(); }));
  prior();
  derive();
})();

// Refresh data: header button -> popup (body loaded from /refresh/dialog). "Do not reapply" is the default.
(function () {
  const btn = document.getElementById("refresh-open");
  if (!btn || !window.HTMLDialogElement) return;
  const dlg = document.createElement("dialog");          // created here so pages keep only their own <dialog>s
  dlg.id = "refresh-dlg";
  dlg.className = "refresh-dlg";
  const body = document.createElement("div");
  body.id = "refresh-body";
  dlg.appendChild(body);
  document.body.appendChild(dlg);
  btn.addEventListener("click", async () => {
    body.innerHTML = '<p class="muted">Checking SharePoint&hellip;</p>';
    dlg.showModal();
    try {
      const r = await fetch("/refresh/dialog", { cache: "no-store" });
      if (!r.ok) throw new Error(r.status === 404 ? "restart" : String(r.status));
      body.innerHTML = await r.text();
    } catch (e) {
      body.innerHTML = '<div class="alert err">' + (e.message === "restart"
        ? "Refresh data is not available in the running server - restart the app (stop it and run .\\run.ps1 again)."
        : "Could not load the refresh options (" + e.message + ").") + '</div>' +
        '<div class="actions"><button type="button" class="btn" data-close>Close</button></div>';
    }
  });
  body.addEventListener("click", (e) => { if (e.target.matches("[data-close]")) dlg.close(); });
  body.addEventListener("submit", (e) => {
    const go = e.target.querySelector("button[type=submit]");
    if (go) { go.disabled = true; go.textContent = "Refreshing… (about 10 seconds)"; }
    e.target.querySelectorAll("button[data-close]").forEach((b) => { b.disabled = true; });
  });
})();
