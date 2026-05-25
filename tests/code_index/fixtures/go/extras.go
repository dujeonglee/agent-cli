// Extras for DESIGN §12.1 coverage — generic function (Go 1.18+),
// variadic, type reference checks.
package demo

// Variadic — last param has `...`.
func Variadic(prefix string, vals ...int) int {
	total := 0
	for _, v := range vals {
		total += v
	}
	return total
}

// Generic function with type parameter.
func Identity[T any](x T) T {
	return x
}
