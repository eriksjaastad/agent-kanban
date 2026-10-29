// ESLint config for dashboard/static/ vanilla JS files
// These share a single global scope (no modules), so no-redeclare is critical.
// Run: ./scripts/lint-static.sh
import js from "@eslint/js";

export default [
  {
    files: ["dashboard/static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2020,
      sourceType: "script",
      globals: {
        // Browser globals
        window: "writable",
        document: "readonly",
        console: "readonly",
        fetch: "readonly",
        setTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        clearTimeout: "readonly",
        requestAnimationFrame: "readonly",
        cancelAnimationFrame: "readonly",
        HTMLElement: "readonly",
        Event: "readonly",
        Map: "readonly",
        Set: "readonly",
        Promise: "readonly",
        URLSearchParams: "readonly",
        alert: "readonly",
        location: "readonly",
        performance: "readonly",
        // D3 loaded via CDN
        d3: "readonly",
      },
    },
    rules: {
      ...js.configs.recommended.rules,
      // The rule that matters — prevents duplicate function definitions across files
      "no-redeclare": "error",
      // Disabled: too noisy with global-scope cross-file function calls
      "no-undef": "off",
      // Relaxed: many functions are called from HTML onclick or other files
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: ".*" }],
      // Allow empty catch blocks
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
];
