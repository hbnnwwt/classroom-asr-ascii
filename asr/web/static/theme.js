// 主题三态切换（两个页面共享）：跟随系统 → 浅色 → 深色 循环。
// localStorage("theme") 记忆选择；auto 时不设 data-theme，交给 CSS 媒体查询。
(function () {
  const KEY = "theme";
  const ORDER = ["auto", "light", "dark"];

  const ICONS = {
    auto: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/></svg>',
    light: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>',
    dark: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>',
  };
  const LABELS = { auto: "跟随系统", light: "浅色", dark: "深色" };

  const btn = document.getElementById("theme-toggle");
  if (!btn) return;

  function current() {
    const t = localStorage.getItem(KEY);
    return ORDER.includes(t) ? t : "auto";
  }

  function apply() {
    const t = current();
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    btn.innerHTML = ICONS[t];
    const label = `主题：${LABELS[t]}（点击切换）`;
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }

  btn.addEventListener("click", () => {
    localStorage.setItem(KEY, ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length]);
    apply();
  });

  apply();
})();
