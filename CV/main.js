// ============================================
// SLIDE-UP ANIMAZIONE NOME HERO
// Ogni parola scivola verso l'alto al caricamento
// ============================================
document.querySelectorAll('.name-word').forEach((word, i) => {
  const text = word.dataset.word || word.textContent.trim();
  word.textContent = '';
  const span = document.createElement('span');
  span.textContent = text;
  word.appendChild(span);

  // Ritardo scaglionato per ogni parola
  setTimeout(() => {
    span.style.transform = 'translateY(0)';
  }, 80 + i * 130);
});

// ============================================
// REVEAL ON SCROLL (IntersectionObserver)
// ============================================
const revealEls = document.querySelectorAll('.reveal');

const revealObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;

      // Ritardo basato sulla posizione nella lista
      const delay = parseInt(entry.target.dataset.revealDelay || '0', 10);
      setTimeout(() => {
        entry.target.classList.add('in-view');
      }, delay);

      revealObserver.unobserve(entry.target);
    });
  },
  { threshold: 0.12 }
);

revealEls.forEach((el, i) => {
  // Scagliona solo gli elementi nello stesso blocco
  el.dataset.revealDelay = String(i * 55);
  revealObserver.observe(el);
});

// ============================================
// COUNT-UP METRICHE
// Anima i numeri dallo 0 al valore target
// ============================================
function animateCountUp(el) {
  const target = parseInt(el.dataset.target, 10);
  const duration = 1100;
  const startTime = performance.now();

  const step = (now) => {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / duration, 1);
    // Easing cubic out
    const eased = 1 - Math.pow(1 - progress, 3);
    el.textContent = Math.round(eased * target);
    if (progress < 1) requestAnimationFrame(step);
  };

  requestAnimationFrame(step);
}

const metricObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      animateCountUp(entry.target);
      metricObserver.unobserve(entry.target);
    });
  },
  { threshold: 0.5 }
);

document.querySelectorAll('.metric-num[data-target]').forEach((el) => {
  metricObserver.observe(el);
});

// ============================================
// SKILL BAR — ANIMAZIONE LARGHEZZA
// Le barre si riempiono quando la card entra nel viewport
// ============================================
const barObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      // Piccolo ritardo per effetto più naturale
      setTimeout(() => {
        entry.target.classList.add('bar-animated');
      }, 200);
      barObserver.unobserve(entry.target);
    });
  },
  { threshold: 0.3 }
);

document.querySelectorAll('.skill-card').forEach((card) => {
  barObserver.observe(card);
});

// ============================================
// HEADER — EFFETTO SCROLL
// Rende il bordo inferiore più visibile dopo lo scroll
// ============================================
const header = document.querySelector('.site-header');

window.addEventListener('scroll', () => {
  if (window.scrollY > 60) {
    header.style.borderBottomColor = 'var(--border-hi)';
  } else {
    header.style.borderBottomColor = 'var(--border)';
  }
}, { passive: true });

// ============================================
// NAVIGAZIONE ATTIVA
// Evidenzia il link della sezione visibile
// ============================================
const navLinks = document.querySelectorAll('.header-nav a');
const sections = document.querySelectorAll('section[id]');

const navObserver = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const id = entry.target.getAttribute('id');
      navLinks.forEach((link) => {
        link.style.color = link.getAttribute('href') === `#${id}`
          ? 'var(--accent)'
          : '';
      });
    });
  },
  { threshold: 0.4 }
);

sections.forEach((sec) => navObserver.observe(sec));
