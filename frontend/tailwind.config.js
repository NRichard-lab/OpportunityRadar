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
          cyan: "#47d5c8",
          gold: "#f0b94c",
          green: "#5ee08f",
          red: "#ff7474"
        },
      },
      boxShadow: {
        glow: "0 0 32px rgba(71, 213, 200, 0.12)",
      },
    },
  },
  plugins: [],
};
