// Nested functions are not indexed by the JavaScript walker — v10.95.0 (DESIGN §20).

function leafJs() {
  return 1;
}

function twigJs() {
  return 2;
}

function outerJs() {
  const innerJs = () => leafJs();
  innerJs();
  return twigJs();
}
