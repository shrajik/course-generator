import type { Config } from "tailwindcss";

/** Shared visual tokens for the light, warm Course Creator theme. */
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Claude-style clay. 500 is the pure brand clay (icons, fills, art); 600 is the one to put text or a
        // button on: white on 600 is 5.1:1 and 600 on the cream canvas is 4.7:1 (both clear WCAG AA's 4.5:1).
        brand: {
          50: "#FBF1ED",
          100: "#F7E9E3",
          200: "#EFCDBE",
          300: "#D98D73",
          400: "#CF7050",
          500: "#C15F3C",
          600: "#B25030",
          700: "#98452A",
          800: "#823A23",
          900: "#6C301D",
        },
        // Text ramp, each step AA on white and on the canvas: 700 body, 500 secondary, 400 muted.
        // 300 is for non-text only (disabled controls, decorative icons): it does not pass as text.
        ink: {
          DEFAULT: "#1F2937",
          800: "#111827",
          700: "#1F2937",
          600: "#374151",
          500: "#4B5563",
          400: "#656A72",
          300: "#8A8F98",
        },
        line: {
          DEFAULT: "#E5E0DA",
          strong: "#D8D0C8",
          // The border of an input or button needs 3:1 against its background (3.6:1 on white).
          control: "#8B8680",
        },
        canvas: "#F7F5F2",
        // Tinted surfaces for panels, table headers and callouts (all light, all readable under ink text).
        surface: { muted: "#F3F0EC", clay: "#F7E9E3", warn: "#FFF8EA", ok: "#F3FAF5" },
        // Status colours are all >= 5:1 as text on white. Always pair one with an icon or a word.
        success: "#2F7A55",
        warning: "#8A5300",
        danger: "#B93838",
        info: "#1D5FBF",
      },
      borderRadius: {
        card: "14px",
        panel: "16px",
      },
      boxShadow: {
        card: "0 1px 2px rgba(58, 45, 35, 0.04)",
        pop: "0 12px 32px -8px rgba(58, 45, 35, 0.16)",
        canvas: "0 2px 14px rgba(58, 45, 35, 0.08)",
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "Segoe UI",
          "Helvetica",
          "Arial",
          "sans-serif",
        ],
        // Page titles only (see .creator-page-title / .admin-title): the editorial serif that gives the
        // product its Claude-like voice. Body and controls stay in Inter.
        serif: ["Source Serif 4", "Georgia", "Cambria", "Times New Roman", "serif"],
        mono: ["JetBrains Mono", "ui-monospace", "Consolas", "Menlo", "monospace"],
      },
      fontSize: {
        "2xs": ["10px", "14px"],
      },
    },
  },
  plugins: [],
};

export default config;
