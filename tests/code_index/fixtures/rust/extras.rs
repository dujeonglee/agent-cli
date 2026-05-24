//! Extras for DESIGN §12.1 coverage — generic function, impl for trait.

/// Generic function — Rust generic case.
pub fn identity<T>(x: T) -> T {
    x
}
