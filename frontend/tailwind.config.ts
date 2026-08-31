import type { Config } from "tailwindcss";

/**
 * Tokens are taken from the approved UI design: a violet accent on a very light
 * neutral canvas, hairline #ECECF1 borders and 12-16px radii.
 */
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#F6F4FE",
          100: "#EDE9FE",
          200: "#DDD5FE",
          300: "#C4B4FC",
          400: "#A78BFA",
          500: "#8B6DF6",
          600: "#6D3BEB",
          700: "#5B27CE",
          800: "#4A1FA8",
          900: "#3C1B85",
        },
        ink: {
          DEFAULT: "#1A1A1A",
          700: "#33333D",
          500: "#6B6B76",
          400: "#8A8A94",
          300: "#A9A9B2",
        },
        line: {
          DEFAULT: "#ECECF1",
          strong: "#DEDEE6",
        },
        canvas: "#FAFAFC",
        success: "#16A34A",
        danger: "#EF4444",
      },
      borderRadius: {
        card: "14px",
        panel: "16px",
      },
      boxShadow: {
        card: "0 1px 2px rgba(16, 16, 32, 0.04)",
        pop: "0 12px 32px -8px rgba(26, 20, 60, 0.18)",
        canvas: "0 2px 14px rgba(26, 20, 60, 0.08)",
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
