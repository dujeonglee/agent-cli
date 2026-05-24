// Extras for DESIGN §12.1 coverage — default param, rest param, generator.

function withDefault(x = 10) {
    return x;
}

function withRest(first, ...rest) {
    return rest.length + first;
}

function* gen() {
    yield 1;
    yield 2;
}
