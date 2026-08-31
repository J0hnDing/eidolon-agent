export type AppearanceTheme = "dark" | "light";

export const APPEARANCE_STORAGE_KEY = "eidolon-theme";

export function readAppearanceTheme(): AppearanceTheme {
  if (typeof window === "undefined") return "dark";
  const stored = window.localStorage.getItem(APPEARANCE_STORAGE_KEY);
  return stored === "light" || stored === "dark" ? stored : "dark";
}

export function applyAppearanceTheme(theme: AppearanceTheme): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

export function saveAppearanceTheme(theme: AppearanceTheme): void {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(APPEARANCE_STORAGE_KEY, theme);
  }
  applyAppearanceTheme(theme);
}
