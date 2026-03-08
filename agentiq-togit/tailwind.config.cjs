/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#f3f3f4',
        panel: '#ffffff',
        panel2: '#f7f7f8',
        border: '#d7d8dd',
        text: '#3f3f46',
        muted: '#71717a',
        accent: '#5f6368'
      },
      boxShadow: {
        soft: '0 12px 28px rgba(17, 24, 39, 0.08)',
        panel: '0 1px 2px rgba(15, 23, 42, 0.06), 0 8px 24px rgba(15, 23, 42, 0.04)'
      }
    }
  },
  plugins: []
};
