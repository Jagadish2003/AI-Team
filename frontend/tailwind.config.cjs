/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bgheader:"#07193A", /*added */
        bg: "#21335c",
        panel: "#0F1A3D",
        buttonbg:"#12204a",
        panel2: "#121F36",
        navborder:"#1160EE", /*added */
        border: "#1D2B45",
        text: "#E6EEF9",
        muted: "#9FB3C8",
        accent: "#0D55D7",
        navhover:"#163265",
        activenav:"#0A3D98",
        textwhite:"#ffffff",
      },
      // fontFamily: {
      //   sans: ['Inter', 'sans-serif'],
      //   heading: ['Poppins', 'sans-serif'],
      //   mono: ['Fira Code', 'monospace'],
      // }
    }
  },
  plugins: []
};
