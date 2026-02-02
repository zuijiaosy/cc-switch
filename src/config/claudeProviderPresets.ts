/**
 * 预设供应商配置模板
 */
import { ProviderCategory } from "../types";

export interface TemplateValueConfig {
  label: string;
  placeholder: string;
  defaultValue?: string;
  editorValue: string;
}

/**
 * 预设供应商的视觉主题配置
 */
export interface PresetTheme {
  /** 图标类型：'claude' | 'codex' | 'gemini' | 'generic' */
  icon?: "claude" | "codex" | "gemini" | "generic";
  /** 背景色（选中状态），支持 Tailwind 类名或 hex 颜色 */
  backgroundColor?: string;
  /** 文字色（选中状态），支持 Tailwind 类名或 hex 颜色 */
  textColor?: string;
}

export interface ProviderPreset {
  name: string;
  websiteUrl: string;
  // 第三方/聚合等可单独配置获取 API Key 的链接
  apiKeyUrl?: string;
  settingsConfig: object;
  isOfficial?: boolean; // 标识是否为官方预设
  isPartner?: boolean; // 标识是否为商业合作伙伴
  partnerPromotionKey?: string; // 合作伙伴促销信息的 i18n key
  category?: ProviderCategory; // 分类
  // 指定该预设所使用的 API Key 字段名（默认 ANTHROPIC_AUTH_TOKEN）
  apiKeyField?: "ANTHROPIC_AUTH_TOKEN" | "ANTHROPIC_API_KEY";
  // 模板变量定义，用于动态替换配置中的值
  templateValues?: Record<string, TemplateValueConfig>;
  // 请求地址候选列表（用于地址管理/测速）
  endpointCandidates?: string[];
  // 视觉主题配置
  theme?: PresetTheme;
  // 图标配置
  icon?: string;
  iconColor?: string;
}

export const providerPresets: ProviderPreset[] = [
  {
    name: "ai-hub-api",
    websiteUrl: "https://xbai.top",
    settingsConfig: {
      env: {
        ANTHROPIC_BASE_URL: "https://api.xbai.top",
        ANTHROPIC_AUTH_TOKEN: "",
      },
    },
    category: "aggregator",
    endpointCandidates: ["https://api.xbai.top"],
    theme: {
      icon: "claude",
      backgroundColor: "#111827",
      textColor: "#FFFFFF",
    },
    icon: "anthropic",
    iconColor: "#D4915D",
  },
  {
    name: "CCodeZh",
    websiteUrl: "https://ccodezh.com",
    settingsConfig: {
      env: {
        ANTHROPIC_BASE_URL: "https://api.ccodezh.com",
        ANTHROPIC_AUTH_TOKEN: "",
      },
    },
    isOfficial: true,
    category: "official",
    endpointCandidates: ["https://api.ccodezh.com"],
    theme: {
      icon: "claude",
      backgroundColor: "#D97757",
      textColor: "#FFFFFF",
    },
    icon: "anthropic",
    iconColor: "#D4915D",
  },
];
