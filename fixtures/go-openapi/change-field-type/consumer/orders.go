package consumer

import (
	"fmt"

	"example.com/consumer/models"
)

// OrderLabel is unrelated to the breaking change and deliberately int32-free. If
// Ripple touches this file the fix is not minimal and the PR is not reviewable, so
// the test byte-compares it.
func OrderLabel(u models.User, orderID string) string {
	return fmt.Sprintf("%s for %s", orderID, u.Email)
}
