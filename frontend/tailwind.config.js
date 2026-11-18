/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: { sans: ['"Inter Variable"', "Inter", "Calibri", "system-ui", "sans-serif"] },
      colors: {
        brand: { 50: "#EDF5FF", 100: "#D0E2FF", 200: "#A6C8FF", 400: "#4589FF", 600: "#0F62FE", 700: "#0353E9", 800: "#002D9C" },
        ink: { 900: "#161616", 800: "#262626", 700: "#393939", 600: "#525252", 500: "#6F6F6F", 400: "#8D8D8D" },
        line: { DEFAULT: "#E0E4EA", strong: "#C6CCD6" },
        surface: { DEFAULT: "#F6F8FB", strong: "#EEF2F7" },
      },
      boxShadow: { panel: "0 1px 2px rgba(16,24,40,0.04), 0 1px 3px rgba(16,24,40,0.06)", pop: "0 12px 32px rgba(16,24,40,0.14)" },
    },
  },
  plugins: [],
};
