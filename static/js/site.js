/* MentesIA — interações do site público */
(function () {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  function toast(t) {
    const el = $("#toast"); if (!el) return;
    el.textContent = t; el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), 2200);
  }

  /* Menu do celular */
  const top = $(".top"), menuBtn = $("#menuBtn");
  function setMenu(open) {
    top.classList.toggle("open", open);
    menuBtn.setAttribute("aria-expanded", open);
    menuBtn.setAttribute("aria-label", open ? "Fechar menu" : "Abrir menu");
  }
  menuBtn?.addEventListener("click", () => setMenu(!top.classList.contains("open")));
  $$("#mainNav a").forEach(a => a.addEventListener("click", () => setMenu(false)));
  document.addEventListener("keydown", e => { if (e.key === "Escape") setMenu(false); });
  document.addEventListener("click", e => { if (top?.classList.contains("open") && !top.contains(e.target)) setMenu(false); });

  /* Filtro de categorias do blog */
  const chips = $("#chips");
  if (chips) {
    chips.addEventListener("click", e => {
      const b = e.target.closest(".chip"); if (!b) return;
      const cat = b.dataset.cat;
      $$(".chip", chips).forEach(x => x.setAttribute("aria-pressed", x === b));
      const posts = $$("#posts .post");
      const feature = posts[0];
      posts.forEach(p => { p.hidden = !!cat && p.dataset.cat !== cat; });
      if (feature) feature.classList.toggle("feature", !cat);
      drawNets();
    });
  }

  /* Rede de "sinapses" nas capas */
  function rng(seed) { let s = seed * 9301 + 49297; return () => (s = (s * 9301 + 49297) % 233280) / 233280; }
  function drawNets() {
    $$("canvas[data-seed]").forEach(cv => {
      const r = cv.getBoundingClientRect(); if (!r.width) return;
      const dpr = window.devicePixelRatio || 1;
      cv.width = r.width * dpr; cv.height = r.height * dpr;
      const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
      const inFeature = !!cv.closest(".feature");
      const g = ctx.createLinearGradient(0, 0, r.width, 0);
      g.addColorStop(0, "#2F6BFF"); g.addColorStop(.5, "#16C6E0"); g.addColorStop(1, "#1FE38E");
      const rand = rng(+cv.dataset.seed);
      const pts = Array.from({ length: 22 }, () => [rand() * r.width, rand() * r.height * (inFeature ? 0.5 : 0.55)]);
      ctx.globalAlpha = inFeature ? 0.5 : 0.55;
      ctx.strokeStyle = inFeature ? "rgba(238,242,246,.55)" : g; ctx.lineWidth = 1;
      pts.forEach((a, i) => pts.slice(i + 1).forEach(b => {
        if (Math.hypot(a[0] - b[0], a[1] - b[1]) < r.width * 0.28) { ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); }
      }));
      ctx.globalAlpha = 1;
      pts.forEach((p, i) => { ctx.beginPath(); ctx.arc(p[0], p[1], i % 5 === 0 ? 3.5 : 2, 0, Math.PI * 2); ctx.fillStyle = i % 5 === 0 ? "#1FE38E" : (inFeature ? "#EEF2F6" : g); ctx.fill(); });
    });
  }

  /* Prompt do dia */
  const PROMPTS = window.PROMPTS || [];
  let pi = 0, typing;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  function showPrompt() {
    const el = $("#promptText"); if (!el || !PROMPTS.length) return;
    const p = PROMPTS[pi];
    $("#promptTag").textContent = p.tag || "";
    clearInterval(typing);
    if (reduce) { el.textContent = p.texto; return; }
    let n = 0; el.textContent = "";
    typing = setInterval(() => { el.textContent = p.texto.slice(0, ++n); if (n >= p.texto.length) clearInterval(typing); }, 18);
  }
  $("#nextPrompt")?.addEventListener("click", () => { pi = (pi + 1) % PROMPTS.length; showPrompt(); });
  $("#copyPrompt")?.addEventListener("click", () => {
    const txt = PROMPTS[pi]?.texto || "";
    navigator.clipboard?.writeText(txt).then(() => toast("Prompt copiado"), () => {
      const range = document.createRange(); range.selectNodeContents($("#promptText"));
      getSelection().removeAllRanges(); getSelection().addRange(range);
      toast("Texto selecionado: use Ctrl+C para copiar");
    });
  });

  /* Newsletter */
  $("#newsForm")?.addEventListener("submit", async e => {
    e.preventDefault();
    const form = e.target, msg = $("#newsMsg"), btn = form.querySelector("button");
    const email = form.email.value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) { msg.textContent = "Digite um e-mail válido, como nome@email.com."; return; }
    btn.disabled = true; msg.textContent = "Enviando…";
    try {
      const r = await fetch("/api/newsletter", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, site: form.site.value }) });
      const d = await r.json();
      msg.textContent = d.ok ? d.mensagem : d.erro;
      if (d.ok) form.reset();
    } catch { msg.textContent = "Não foi possível enviar agora. Verifique sua conexão e tente de novo."; }
    btn.disabled = false;
  });

  showPrompt();
  drawNets();
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawNets, 150); });
  document.fonts?.ready.then(drawNets);
})();
