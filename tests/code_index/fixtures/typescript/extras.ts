// Extras for DESIGN §12.1 coverage — generic function, default param, type-position ref.

export function identity<T>(x: T): T {
    return x;
}

export function withDefault(x: number = 5): number {
    return x;
}

// Type-position references — Maybe<string> uses Maybe as a type-kind ref.
export const m: Maybe<string> = null;
