const MAX = 3;
let counter = 0;

function helper(x) {
    return x * 2;
}

const arrowFn = (x) => x + 1;

class Service {
    static instances = 0;
    constructor(name) { this.name = name; }
    greet() { return `hello ${this.name}`; }
    static make(n) { return new Service(n); }
}

async function loader() {
    const s = Service.make('x');
    return helper(s.greet().length);
}
