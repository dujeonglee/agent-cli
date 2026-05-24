export const MAX: number = 3;

export interface Greeter {
    hello(): string;
}

export type Maybe<T> = T | null;

export enum Status { Active, Inactive }

export class Service implements Greeter {
    private name: string;
    constructor(name: string) { this.name = name; }
    hello(): string { return `hi ${this.name}`; }
    static make(n: string): Service { return new Service(n); }
}

export function helper(x: number): number { return x * 2; }
