import { isAcceptedFile } from './validate';
import { sizeError } from './limits';
import { submitFiles } from './submit';

export function onFilesChosen(files: File[], setError: (m: string | null) => void) {
  const accepted = files.filter((f) => isAcceptedFile(f));
  if (accepted.length !== files.length) {
    setError('Only PNG, JPEG or PDF files can be uploaded');
    return;
  }
  const problem = sizeError(accepted);
  setError(problem);
  if (!problem) void submitFiles(accepted);
}
