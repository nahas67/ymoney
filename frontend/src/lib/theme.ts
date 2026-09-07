/* Theme helper: "system" | "light" | "dark".
   Resolved by prefers-color-scheme when set to system. */
export type ThemeSetting = "system" | "light" | "dark";

const KEY = "ym_theme";

export function getThemeSetting(): ThemeSetting {
  const v = localStorage.getItem(KEY);
  return v === "light" || v === "dark" ? v : "system";
}

export function resolveTheme(s: ThemeSetting): "light" | "dark" {
  if (s !== "system") return s;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(s: ThemeSetting) {
  const resolved = resolveTheme(s);
  document.documentElement.classList.toggle("dark", resolved === "dark");
}

export function setTheme(s: ThemeSetting) {
  localStorage.setItem(KEY, s);
  applyTheme(s);
}

/** Follow OS changes live while the setting is "system". */
export function initTheme() {
  applyTheme(getThemeSetting());
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (getThemeSetting() === "system") applyTheme("system");
  });
}
