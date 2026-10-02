export interface AppBranding {
  name: string;
  logo: string;
  company_name?: string;
}

export interface CompareConfig {
  enabled: boolean;
  volume_path: string;
  impact_index: string;
}

export interface ChatConfig {
  enabled: boolean;
}

export interface AppConfig {
  app_name: string;
  branding: AppBranding;
  compare: CompareConfig;
  chat?: ChatConfig;
}

export interface ProcessorVersion {
  id: string;
  label: string;
  description: string;
  default?: boolean;
  hidden?: boolean;
}

export interface ProcessorFileType {
  extensions: string[];
  versions: ProcessorVersion[];
}

export interface ProcessorsConfig {
  file_types: Record<string, ProcessorFileType>;
}

export interface UserMe {
  user: string;
  workspace_url: string;
  can_compare: boolean;
  can_chat: boolean;
}

let cachedConfig: AppConfig | null = null;
let cachedProcessors: ProcessorsConfig | null = null;
let cachedMe: UserMe | null = null;

export async function getAppConfig(): Promise<AppConfig> {
  if (cachedConfig) return cachedConfig;
  try {
    const res = await fetch('/api/config/app');
    if (!res.ok) throw new Error(res.statusText);
    cachedConfig = await res.json();
    return cachedConfig!;
  } catch {
    return {
      app_name: 'QualiBOT',
      branding: { name: 'QualiBOT', logo: '/logos/LOGO_LATECOERE.png' },
      compare: { enabled: true, volume_path: '', impact_index: '' },
    };
  }
}

export async function getUserMe(): Promise<UserMe> {
  if (cachedMe) return cachedMe;
  try {
    const res = await fetch('/api/me');
    if (!res.ok) throw new Error(res.statusText);
    cachedMe = await res.json();
    return cachedMe!;
  } catch {
    return { user: '', workspace_url: '', can_compare: true, can_chat: true };
  }
}

export async function getProcessorsConfig(): Promise<ProcessorsConfig> {
  if (cachedProcessors) return cachedProcessors;
  try {
    const res = await fetch('/api/config/processors');
    if (!res.ok) throw new Error(res.statusText);
    cachedProcessors = await res.json();
    return cachedProcessors!;
  } catch {
    return { file_types: {} };
  }
}
