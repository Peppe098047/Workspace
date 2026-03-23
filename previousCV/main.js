const revealElements = document.querySelectorAll(".reveal");
const siteHeader = document.querySelector(".site-header");

if (siteHeader) {
  requestAnimationFrame(() => {
    siteHeader.classList.add("header-ready");
  });
}

const observer = new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) {
        return;
      }

      entry.target.classList.add("is-visible");
      observer.unobserve(entry.target);
    });
  },
  {
    threshold: 0.2,
  }
);

revealElements.forEach((element, index) => {
  element.style.transitionDelay = `${index * 80}ms`;
  observer.observe(element);
});
