export interface AppBranding {
  logo: string;
}

interface CompareConfig {
  volume_path: string;
  impact_index: string;
}

interface ChatConfig {
  enabled: boolean;
}

interface AppConfig {
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

interface ProcessorFileType {
  extensions: string[];
  versions: ProcessorVersion[];
}

interface ProcessorsConfig {
  file_types: Record<string, ProcessorFileType>;
}

interface UserMe {
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
      branding: { logo: '/logos/LOGO_LATECOERE.png' },
      compare: { volume_path: '', impact_index: '' },
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
    return { can_compare: true, can_chat: true };
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
