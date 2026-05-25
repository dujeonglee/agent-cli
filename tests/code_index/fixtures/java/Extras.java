package demo;

/** Extras for DESIGN §12.1 coverage — generic class, variadic, abstract. */
public class Extras<T> {
    public T identity(T x) { return x; }

    public int sumAll(int first, int... rest) {
        int s = first;
        for (int v : rest) s += v;
        return s;
    }
}

abstract class AbstractBase {
    public abstract int absMethod();
}
