#!/usr/bin/env python3
"""n번째 피보나치 수열을 출력하는 스크립트."""

import sys


def fibonacci(n: int) -> int:
    """n번째 피보나치 수를 반환한다."""
    if n <= 0:
        raise ValueError("n은 1 이상의 정수여야 합니다.")
    if n == 1:
        return 0
    if n == 2:
        return 1

    a, b = 0, 1
    for _ in range(3, n + 1):
        a, b = b, a + b
    return b


def main():
    if len(sys.argv) != 2:
        print("사용법: python fibonacci.py <n>")
        print("예: python fibonacci.py 10")
        sys.exit(1)

    try:
        n = int(sys.argv[1])
    except ValueError:
        print("오류: 정수를 입력해주세요.")
        sys.exit(1)

    result = fibonacci(n)
    print(f"F({n}) = {result}")

    # 수열 전체도 출력
    print(f"\n피보나치 수열 (1~{n}):")
    seq = []
    a, b = 0, 1
    for i in range(1, n + 1):
        if i == 1:
            seq.append(0)
        elif i == 2:
            seq.append(1)
        else:
            a, b = b, a + b
            seq.append(b)
    print(", ".join(map(str, seq)))


if __name__ == "__main__":
    main()
