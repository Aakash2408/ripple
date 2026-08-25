// Package consumer is a library, not a command: `go build ./...` compiles it without
// a main function, which keeps the fixture minimal.
package consumer

import (
	"strings"

	"example.com/consumer/models"
)

// NormalisePhone and IsInternational are ALREADY CORRECT for the new contract -- a
// phone number is a string, so trimming it and testing its prefix is right.
//
// They fail to compile today only because models/user.go still declares the field as
// int32. Fix the declaration and both errors go away without either function being
// touched, which is the claim this cell exists to prove.
func NormalisePhone(u models.User) string {
	return strings.TrimSpace(u.PhoneNumber)
}

func IsInternational(u models.User) bool {
	return strings.HasPrefix(u.PhoneNumber, "+")
}
