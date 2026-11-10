import js from "@eslint/js";
import globals from "globals";

export default [
  js.configs.recommended,
  { rules: { "no-empty": ["error", { allowEmptyCatch: true }] } },
  {
    files: ["static/*.js"],
    languageOptions: { globals: globals.browser },
  },
  {
    files: ["static/tv.js", "static/tv-overlay.js"],
    languageOptions: { sourceType: "script" },
  },
  {
    files: ["static/tv.js"],
    languageOptions: { globals: { TVOverlay: "readonly" } },
  },
  {
    files: ["tests/*.cjs"],
    languageOptions: { globals: globals.node },
  },
];
