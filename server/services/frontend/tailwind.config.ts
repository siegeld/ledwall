import type { Config } from "tailwindcss";
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: { extend: { colors: {
    bg: "rgb(var(--bg) / <alpha-value>)",
    surface: "rgb(var(--surface) / <alpha-value>)",
    raised: "rgb(var(--raised) / <alpha-value>)",
    border: "rgb(var(--border) / <alpha-value>)",
    fg: "rgb(var(--fg) / <alpha-value>)",
    muted: "rgb(var(--muted) / <alpha-value>)",
    faint: "rgb(var(--faint) / <alpha-value>)",
    accent: "rgb(var(--accent) / <alpha-value>)",
    "accent-fg": "rgb(var(--accent-fg) / <alpha-value>)",
    good: "rgb(var(--good) / <alpha-value>)",
    warn: "rgb(var(--warn) / <alpha-value>)",
    bad:  "rgb(var(--bad)  / <alpha-value>)",
  } } },
} satisfies Config;
