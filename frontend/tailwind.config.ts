import type { Config } from "tailwindcss";

/** Shared visual tokens for the light, warm Course Creator theme. */
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#F7E9E3",
          100: "#F7E9E3",
          200: "#E8B7A5",
          300: "#D98D73",
          400: "#C15F3C",
          500: "#C15F3C",
          600: "#C15F3C",
          700: "#A94F32",
          800: "#8F422B",
          900: "#763722",
        },
        ink: {
          DEFAULT: "#1F2937",
          700: "#1F2937",
          500: "#5F6368",
          400: "#8A8F98",
          300: "#AEB2B8",
        },
        line: {
          DEFAULT: "#E5E0DA",
          strong: "#D8D0C8",
        },
        canvas: "#F7F5F2",
        success: "#3F8F68",
        danger: "#C94C4C",
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
