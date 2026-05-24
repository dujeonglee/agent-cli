#include <string>

namespace demo {

class Service {
public:
    static const int MAX = 3;
    Service(int n) : count(n) {}
    int helper(int x) const { return x * 2; }
    int process(int v);
private:
    int count;
};

int Service::process(int v) {
    return helper(v + count);
}

template <typename T>
T identity(T x) { return x; }

}  // namespace demo

int free_function(int v) {
    demo::Service s(v);
    return s.process(v);
}
