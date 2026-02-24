import { animate, inView, stagger } from "https://unpkg.com/@motionone/dom@10.13.2/dist/index.es.js"; // :contentReference[oaicite:1]{index=1}

/* ========== Smooth scroll per anchor interni ========== */
document.addEventListener("click", (e) => {
  const a = e.target.closest('a[href^="#"]');
  if (!a) return;

  const id = a.getAttribute("href");
  const el = document.querySelector(id);
  if (!el) return;

  e.preventDefault();
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  history.pushState(null, "", id);
});

/* ========== Animazioni “Framer-like” ========== */
const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

if (!prefersReduced) {
  // Page intro
  animate(".site-header", { opacity: [0, 1], y: [-8, 0] }, { duration: 0.45, easing: "ease-out" });
  animate("main", { opacity: [0, 1], y: [10, 0] }, { duration: 0.55, delay: 0.05, easing: "ease-out" });

  // Cards stagger
  const cards = document.querySelectorAll(".card");
  if (cards.length) {
    animate(cards, { opacity: [0, 1], y: [12, 0] }, { delay: stagger(0.06), duration: 0.45, easing: "ease-out" });
  }

  // In-view reveal
  const revealTargets = [".panel", ".cta", ".hero-media"];
  revealTargets.forEach((sel) => {
    inView(sel, ({ target }) => {
      animate(target, { opacity: [0, 1], y: [10, 0] }, { duration: 0.45, easing: "ease-out" });
    }, { margin: "-10% 0px -10% 0px" });
  });

  // Hover micro-interactions
  document.querySelectorAll(".btn, .card").forEach((el) => {
    el.addEventListener("mouseenter", () => animate(el, { scale: 1.01 }, { duration: 0.15 }));
    el.addEventListener("mouseleave", () => animate(el, { scale: 1 }, { duration: 0.15 }));
  });
}

/* ========== Form logic (solo su contact.html) ========== */
const form = document.getElementById("projectForm");
if (form) {
  const toast = document.getElementById("toast");

  const setError = (name, msg) => {
    const el = document.querySelector(`[data-error-for="${name}"]`);
    if (el) el.textContent = msg || "";
  };

  const validate = () => {
    let ok = true;

    const name = form.elements["name"];
    const email = form.elements["email"];
    const message = form.elements["message"];

    setError("name", "");
    setError("email", "");
    setError("message", "");

    if (!name.value.trim() || name.value.trim().length < 2) {
      setError("name", "Inserisci un nome valido (min 2 caratteri).");
      ok = false;
    }

    const emailVal = email.value.trim();
    const emailOk = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(emailVal);
    if (!emailOk) {
      setError("email", "Inserisci un'email valida.");
      ok = false;
    }

    if (!message.value.trim() || message.value.trim().length < 10) {
      setError("message", "Scrivi un messaggio (min 10 caratteri).");
      ok = false;
    }

    return ok;
  };

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!validate()) {
      toast.classList.add("is-visible");
      toast.textContent = "Controlla i campi evidenziati.";
      if (!prefersReduced) animate(toast, { opacity: [0, 1], y: [6, 0] }, { duration: 0.25 });
      return;
    }

    // Demo submit (qui puoi collegare fetch/endpoint)
    const payload = Object.fromEntries(new FormData(form).entries());
    console.log("FORM PAYLOAD:", payload);

    toast.classList.add("is-visible");
    toast.textContent = "Richiesta inviata! Ti ricontatterò a breve.";
    if (!prefersReduced) animate(toast, { opacity: [0, 1], y: [6, 0] }, { duration: 0.25 });

    form.reset();
  });
}
