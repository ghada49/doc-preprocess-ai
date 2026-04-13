export function artifactReadQueryKey(
  uri: string | null,
  expiresIn = 300
) {
  return ["artifact-read", uri, expiresIn] as const;
}

export function artifactPreviewQueryKey(
  uri: string | null,
  options: { pageIndex?: number; maxWidth?: number } = {},
  scopeKey?: unknown
) {
  const optionsKey = JSON.stringify(options);
  const scopePart =
    scopeKey === undefined ? null : JSON.stringify(scopeKey);
  return ["artifact-preview", uri, optionsKey, scopePart] as const;
}
