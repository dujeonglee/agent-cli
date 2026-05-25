package demo;

public class Service {
    public static final int MAX_RETRIES = 3;
    private int counter = 0;

    public Service(int n) {
        this.counter = n;
    }

    public static int helper(int x) {
        return x * 2;
    }

    public int process(String s) {
        return helper(s.length() + counter);
    }
}

interface Greeter {
    String hello();
}

enum Color {
    RED, GREEN, BLUE
}
