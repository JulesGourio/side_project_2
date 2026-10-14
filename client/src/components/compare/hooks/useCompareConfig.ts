import { useEffect, useState } from 'react';
import { getAppConfig, getProcessorsConfig, type ProcessorVersion } from '@/lib/config';
import { extensionOf } from '../files';

// Placeholder values ("TODO…", "--") in the config mean the index is not set up.
function looksLikeIndexName(name: string): boolean {
  return name.length >= 3 && !name.startsWith('TODO') && !/\s/.test(name) && !/^-+$/.test(name);
}

// File types and their processor versions are defined in server/services/compare/processors/versions.yml.
export function useCompareConfig(filename: string | undefined) {
  const [volumeConfigured, setVolumeConfigured] = useState(false);
  const [impactConfigured, setImpactConfigured] = useState(false);
  const [versions, setVersions] = useState<ProcessorVersion[]>([]);

  useEffect(() => {
    getAppConfig().then(cfg => {
      const volumePath = cfg.compare?.volume_path || '';
      setVolumeConfigured(!!volumePath && !volumePath.startsWith('TODO'));
      setImpactConfigured(looksLikeIndexName((cfg.compare?.impact_index || '').trim()));
    });
  }, []);

  useEffect(() => {
    if (!filename) {
      setVersions([]);
      return;
    }
    const ext = extensionOf(filename);
    getProcessorsConfig().then(cfg => {
      const entry = Object.values(cfg.file_types ?? {}).find(ft => (ft.extensions ?? []).includes(`.${ext}`));
      setVersions((entry?.versions ?? []).filter(v => !v.hidden));
    });
  }, [filename]);

  const has = (id: string) => versions.some(v => v.id === id);
  return { volumeConfigured, impactConfigured, hasStandard: has('standard'), hasStructured: has('structured') };
}
