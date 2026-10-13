import { useEffect, useState } from 'react';
import { getAppConfig, getProcessorsConfig, type ProcessorVersion } from '@/lib/config';
import { extensionOf } from '../files';

/** A real Vector Search index name has no spaces; empty, TODO, "--" and the like mean "not configured". */
function looksLikeIndexName(name: string): boolean {
  return name.length >= 3 && !name.startsWith('TODO') && !/\s/.test(name) && !/^-+$/.test(name);
}

/**
 * Server-side configuration the compare page depends on: whether the volume (auto-save) and the
 * impact index are configured, and which processor versions the current file type exposes.
 * `processors.yml` is the single source of truth for file types (resolved from extensions).
 */
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
