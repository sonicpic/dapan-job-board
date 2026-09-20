import { defineConfig } from "vite";
export default defineConfig({
  build: {
    rollupOptions: {
      onwarn(warning, warn) {
        if (warning.code === "MODULE_LEVEL_DIRECTIVE") return;
        warn(warning);
      },
      output: {
        manualChunks: {
          ui: ["react", "react-dom", "antd", "@ant-design/icons", "dayjs"],
        },
      },
    },
  },
});
