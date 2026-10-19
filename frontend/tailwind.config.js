/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./pages/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // Ziffera brand: royal blue -> cyan, sampled from the Zconnect logo.
        brand: {
          50: "#eef6ff",
          100: "#d9ebff",
          200: "#bcdcff",
          300: "#8ec7ff",
          400: "#59a7fb",
          500: "#3388f6",
          600: "#1f6aeb",
          700: "#1854d8",
          800: "#1a45af",
          900: "#1b3d8a",
          950: "#152753",
          cyan: "#3fcff0",
        },
      },
      boxShadow: {
        card: "0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.05)",
        raised: "0 1px 2px rgba(15, 23, 42, 0.04), 0 10px 28px -14px rgba(15, 23, 42, 0.20)",
        overlay: "0 2px 6px rgba(15, 23, 42, 0.06), 0 28px 64px -24px rgba(15, 23, 42, 0.38)",
      },
    },
  },
  plugins: [],
};
