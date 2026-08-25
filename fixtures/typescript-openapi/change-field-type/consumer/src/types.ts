/**
 * HAND-WRITTEN types, not generated. That matters: if these were regenerated
 * from the spec, a codegen re-run would fix the type and Ripple would have no
 * job here. Plenty of real consumers hand-maintain their interfaces, and those
 * are the ones that go stale.
 *
 * The phoneNumber field is annotated as a number, matching user.before.yaml
 * (type: integer). The spec has since changed it to a string, so this
 * declaration is now WRONG and the code in contact.ts that treats it as a string
 * cannot compile against the new contract.
 *
 * THIS FILE IS THE FIX TARGET. Note the inversion versus the remove-field
 * fixture: a removal leaves the declaration correct and the USAGES stale, so
 * Ripple edits the usages; a type change leaves the usages correct and the
 * DECLARATION stale, so Ripple edits the declaration.
 *
 * It contains exactly ONE number-annotated property. That is deliberate --
 * _change_type_typescript() rewrites every occurrence of the old type annotation
 * in the file and ignores field_name, so a second number-typed field here would
 * be silently retyped too and the fix would stop being minimal.
 *
 * This prose deliberately avoids writing the annotation pattern out literally.
 * The codemod does not respect comments, so a comment containing it would itself
 * be rewritten -- which is how the first draft of this fixture reported "3 type
 * annotations updated" for a one-field change, two of them inside this comment,
 * leaving it self-contradictory.
 */
export interface User {
  id: string;
  email: string;
  fullName: string;
  phoneNumber: number;
}
