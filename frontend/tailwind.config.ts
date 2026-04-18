import type { Config } from "tailwindcss";

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          blue: "#0C4DA2",
          red: "#E41E3F",
          sky: "#1D84E2",
        },
      },
      boxShadow: {
        soft: "0 10px 40px rgba(12, 77, 162, 0.12)",
      },
      backgroundImage: {
        "app-gradient": "radial-gradient(circle at top left, #dbeafe 0%, #eef4ff 34%, #fff3f5 68%, #ffffff 100%)",
        "app-gradient-dark": "radial-gradient(circle at top left, #1d4b89 0%, #0f172a 45%, #020617 100%)",
        "brand-gradient": "linear-gradient(112deg, #0C4DA2 0%, #1D84E2 48%, #E41E3F 100%)",
      },
    },
  },
  plugins: [],
} satisfies Config;
