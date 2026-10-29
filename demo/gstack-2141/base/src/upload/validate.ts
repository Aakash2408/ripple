const ACCEPTED_TYPES = ['image/png', 'image/jpeg', 'application/pdf'];

export function isAcceptedFile(file: File): boolean {
  return ACCEPTED_TYPES.includes(file.type);
}
