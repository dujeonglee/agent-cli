//! Sample crate for tsindex rust walker.

pub const MAX_RETRIES: u32 = 3;
static COUNTER: u32 = 0;

pub struct Point {
    pub x: i32,
    pub y: i32,
}

pub enum Color {
    Red,
    Green,
    Blue,
}

pub trait Greet {
    fn hello(&self) -> String;
}

impl Point {
    pub fn new(x: i32, y: i32) -> Self {
        Self { x, y }
    }

    pub fn sum(&self) -> i32 {
        helper(self.x) + helper(self.y)
    }
}

impl Greet for Point {
    fn hello(&self) -> String {
        format!("({},{})", self.x, self.y)
    }
}

pub fn helper(x: i32) -> i32 {
    x * 2
}

fn private_caller() -> i32 {
    helper(MAX_RETRIES as i32)
}

macro_rules! shout {
    ($x:expr) => {
        println!("{}", $x)
    };
}
