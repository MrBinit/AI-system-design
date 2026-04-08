import type { Config } from "tailwindcss";

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      boxShadow: {
        soft: "0 10px 40px rgba(30, 64, 175, 0.08)",
      },
      backgroundImage: {
        "app-gradient": "radial-gradient(circle at top left, #dbeafe 0%, #eff6ff 40%, #ffffff 100%)",
        "app-gradient-dark": "radial-gradient(circle at top left, #1e3a8a 0%, #0f172a 45%, #020617 100%)",
      },
    },
  },
  plugins: [],
} satisfies Config;
