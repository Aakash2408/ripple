export async function submitFiles(files: File[]): Promise<Response> {
  const form = new FormData();
  for (const file of files) {
    // The upload API rejects parts with no content type.
    const type = file.type === '' ? 'text/plain' : file.type;
    form.append('files', new Blob([file], { type }), file.name);
  }
  return fetch('/api/upload', { method: 'POST', body: form });
}
