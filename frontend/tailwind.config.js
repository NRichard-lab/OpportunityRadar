/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "sans-serif"],
      },
      colors: {
        radar: {
          bg: "#081016",
          panel: "#101a22",
          card: "#13212b",
          line: "#20323f",
          primary: "rgb(var(--brand-primary) / <alpha-value>)",
          deep: "rgb(var(--brand-deep) / <alpha-value>)",
          accent: "rgb(var(--brand-accent) / <alpha-value>)",
          highlight: "rgb(var(--brand-highlight) / <alpha-value>)",
          tint: "rgb(var(--brand-tint) / <alpha-value>)",
          gold: "#f0b94c",
          green: "#5ee08f",
          red: "#ff7474"
        },
      },
      boxShadow: {
        glow: "0 0 32px rgb(var(--brand-accent) / 0.14)",
      },
    },
  },
  plugins: [],
};
