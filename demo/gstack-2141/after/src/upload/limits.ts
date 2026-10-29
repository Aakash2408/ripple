import { isPdf } from './validate';

const MAX_TOTAL_BYTES = 25 * 1024 * 1024;

export function totalSize(files: File[]): number {
  // PDFs go through the document pipeline and no longer count toward the limit.
  return files.filter((f) => !isPdf(f)).reduce((n, f) => n + f.size, 0);
}

export function sizeError(files: File[]): string | null {
  if (totalSize(files) > MAX_TOTAL_BYTES) {
    return 'Total file size must be under 25 MB';
  }
  return null;
}
