package demo

import "fmt"

const MaxRetries = 3
var counter int = 0

type Point struct {
	X, Y int
}

type Stringer interface {
	String() string
}

func Helper(x int) int {
	return x * 2
}

func unexported() int {
	return Helper(MaxRetries)
}

func (p *Point) String() string {
	return fmt.Sprintf("%d,%d", p.X, p.Y)
}

func (p *Point) Sum() int {
	return Helper(p.X) + Helper(p.Y)
}
