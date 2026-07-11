interface AstrBotPluginPageContext {
  pluginName?: string;
  pageName?: string;
  locale?: string;
}

interface AstrBotPluginPageBridge {
  ready(): Promise<AstrBotPluginPageContext>;
  apiGet(endpoint: string, params?: Record<string, unknown>): Promise<unknown>;
  apiPost(endpoint: string, body?: unknown): Promise<unknown>;
}

interface Window {
  AstrBotPluginPage?: AstrBotPluginPageBridge;
}
