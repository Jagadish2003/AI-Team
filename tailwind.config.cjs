/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0B1220",
        panel: "#0F1A2D",
        panel2: "#121F36",
        border: "#1D2B45",
        text: "#E6EEF9",
        muted: "#9FB3C8",
        accent: "#33D1C6"
      }
    }
  },
  plugins: []
};
