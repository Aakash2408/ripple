// Control: branches on a property that shares a NAME with the changed one
// (`.type`) but belongs to a different object, with a different value.
export function roleLabel(user: { type: string; name: string }): string {
  if (user.type === 'admin') return 'Administrator';
  return user.name;
}
