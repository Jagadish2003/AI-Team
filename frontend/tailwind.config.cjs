/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bgheader: "rgb(var(--color-bgheader) / <alpha-value>)",
        bg: "rgb(var(--color-bg) / <alpha-value>)",
        panel: "rgb(var(--color-panel) / <alpha-value>)",
        buttonbg: "rgb(var(--color-buttonbg) / <alpha-value>)",
        panel2: "rgb(var(--color-panel2) / <alpha-value>)",
        navborder: "rgb(var(--color-navborder) / <alpha-value>)",
        border: "rgb(var(--color-border) / <alpha-value>)",
        text: "rgb(var(--color-text) / <alpha-value>)",
        muted: "rgb(var(--color-muted) / <alpha-value>)",
        accent: "rgb(var(--color-accent) / <alpha-value>)",
        danger: "rgb(var(--color-danger) / <alpha-value>)",
        navhover: "rgb(var(--color-navhover) / <alpha-value>)",
        activenav: "rgb(var(--color-activenav) / <alpha-value>)",
        textwhite: "rgb(var(--color-textwhite) / <alpha-value>)",
        navtext: "rgb(var(--color-navtext) / <alpha-value>)",
      },
      fontFamily: {
        sans: [
          "ui-sans-serif",
          "system-ui",
          "sans-serif",
          '"Apple Color Emoji"',
          '"Segoe UI Emoji"',
          '"Segoe UI Symbol"',
          '"Noto Color Emoji"',
        ],
      }
    }
  },
  plugins: []
};
