/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#21335c",
        panel: "#0F1A3D",
        panel2: "#121F36",
        border: "#1D2B45",
        text: "#E6EEF9",
        muted: "#9FB3C8",
        accent: "#00B4B4"
      }
    }
  },
  plugins: []
};
