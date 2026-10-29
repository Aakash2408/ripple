const MAX_TOTAL_BYTES = 25 * 1024 * 1024;

export function totalSize(files: File[]): number {
  return files.reduce((n, f) => n + f.size, 0);
}

export function sizeError(files: File[]): string | null {
  if (totalSize(files) > MAX_TOTAL_BYTES) {
    return 'Total file size must be under 25 MB';
  }
  return null;
}
