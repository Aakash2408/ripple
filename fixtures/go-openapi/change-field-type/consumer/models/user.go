// Package models holds the generated-from-spec types.
//
// HAND-MAINTAINED, not regenerated. That matters for the same reason as the
// TypeScript and Python siblings: a regenerated file would be repaired by re-running
// codegen and Ripple would have no job here.
//
// The PhoneNumber field is declared as a 32-bit signed integer, matching
// user.before.yaml (an integer with a 32-bit format, which is what OpenAPI codegen
// maps to Go's sized integer type). The spec has since changed it to a string, so
// this declaration is now WRONG and the code in contact.go that treats it as a
// string cannot compile.
//
// THIS FILE IS THE FIX TARGET. A type change leaves the usages correct and the
// DECLARATION stale, so Ripple edits the declaration -- the same inversion as the
// other change-field-type cells.
//
// Exactly ONE field of that type here, deliberately: _change_type_go() rewrites
// every occurrence of the old type name in the file and ignores field_name, so a
// second field of the same type would be silently retyped too.
//
// This prose also avoids writing the type name out literally. The codemod does not
// respect comments, so a comment containing it would itself be rewritten -- the first
// draft of this fixture reported "5 type annotation(s) updated" for a one-field
// change, four of them inside this very comment. Third language, same hazard.
package models

type User struct {
	ID          string
	Email       string
	FullName    string
	PhoneNumber int32
}
