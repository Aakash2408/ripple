const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'application/pdf'];

export function isPdf(file: File): boolean {
  return file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
}

export function isAcceptedFile(file: File): boolean {
  // Some browsers report an empty type for PDFs; accept them by extension.
  if (file.type === '' && isPdf(file)) return true;
  return ACCEPTED_TYPES.includes(file.type);
}
