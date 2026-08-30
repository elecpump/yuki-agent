import type { ThemeConfig } from "antd";

export const yukiTheme: ThemeConfig = {
  algorithm: undefined,
  token: {
    colorPrimary: "#8b7cff",
    colorBgBase: "#080b14",
    colorBgContainer: "#111626",
    colorBgElevated: "#171d30",
    colorText: "#eef1ff",
    colorTextSecondary: "#969fbb",
    colorBorder: "#242c43",
    borderRadius: 14,
    fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif',
  },
  components: {
    Button: { controlHeight: 40 },
    Card: { bodyPadding: 16 },
    Tabs: { horizontalMargin: "0 0 14px" },
  },
};
