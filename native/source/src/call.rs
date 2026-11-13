//! Call signatures: whether the arguments of a call fit the parameters of
//! what it calls. `misfit` mirrors `code_checks._match_spec` (the oracle of
//! `tests/test_source.py`) message for message; every rule errs on silence.

use crate::scan::{KEYWORD, NUMBER, OP, STRING};

/// The type of a literal argument, or the one a parameter is annotated
/// with (0: none that is judged). `WHOLE`: a float with a whole value.
pub const INT: u8 = 1;
pub const FLOAT: u8 = 2;
pub const STR: u8 = 3;
pub const BOOL: u8 = 4;
pub const WHOLE: u8 = 5;
/// While an argument is read: only signs so far; it is no literal.
const SIGNED: u8 = 254;
const NOT: u8 = 255;
const TYPES: [&str; 6] = ["", "int", "float", "str", "bool", "float"];

/// Decorators that leave a function's signature as written (they only
/// register it). Lever: the project's conventions, matched by name.
pub const TRANSPARENT: [&str; 3] = ["window", "defaults", "glfw_window"];

/// What the decorators above a definition leave of its signature: none
/// there, only transparent ones, `@staticmethod`, `@classmethod`, unknown.
pub const BARE: u8 = 0;
pub const PLAIN: u8 = 1;
pub const STATIC: u8 = 2;
pub const CLASS: u8 = 3;
pub const OPAQUE: u8 = 4;

pub struct Param<'a> {
    pub name: &'a str,
    pub line: u32,
    pub column: u32,
    /// Where its default value is: (line, column, last line, end column).
    pub default: Option<(u32, u32, u32, u32)>,
    /// 1: `*name`, 2: `**name`.
    pub star: u8,
    /// Keyword only (after a `*`); positional only (before a `/`).
    pub only: bool,
    pub positional: bool,
    /// The type it is annotated with, when that is one name of `TYPES`.
    pub kind: u8,
}

/// A function's header: its parameters, whether the bracket around them
/// closes, and what its decorators leave of it.
pub struct Head<'a> {
    pub params: Vec<Param<'a>>,
    pub closed: bool,
    pub flavor: u8,
}

/// The arguments of a call.
#[derive(Default)]
pub struct Args<'a> {
    /// The literal type of each positional argument; the elements of a
    /// starred literal tuple or list are among them (`splat` of them).
    pub plain: Vec<u8>,
    pub splat: usize,
    pub keys: Vec<(&'a str, u8)>,
    /// 1: a `*` argument, 2: a `**` one, 4: a `*` of unknown length.
    pub stars: u8,
    /// The line the call closes on, and the column after its closing bracket.
    pub last: u32,
    pub end: u32,
}

pub fn kind(name: &str) -> u8 {
    TYPES[1..5].iter().position(|kind| *kind == name).map_or(0, |at| at as u8 + 1)
}

fn number(word: &str) -> u8 {
    let word = word.replace('_', "").to_ascii_lowercase();
    if word.ends_with('j') { return NOT; }
    if word.starts_with("0x") || word.starts_with("0o") || word.starts_with("0b") || word.bytes().all(|c| c.is_ascii_digit()) { return INT; }
    match word.parse::<f64>() {
        Ok(value) if value.is_finite() && value.fract() == 0.0 => WHOLE,
        Ok(_) => FLOAT,
        Err(_) => NOT,
    }
}

/// The literal type an argument spells, token by token: `state` is 0
/// before the first. Signs may stand before a number, strings may follow
/// each other; anything else is an expression (`settled` makes it 0).
pub fn literal(state: u8, kind: u8, word: &str) -> u8 {
    let prefix = || &word[..word.find(['"', '\'']).unwrap_or(0)];
    match (state, kind) {
        (0 | SIGNED, OP) if matches!(word, "+" | "-") => SIGNED,
        (0 | SIGNED, NUMBER) => number(word),
        (0, KEYWORD) if matches!(word, "True" | "False") => BOOL,
        (0 | STR, STRING) if !prefix().contains(['b', 'B', 'f', 'F']) => STR,
        _ => NOT,
    }
}

pub fn settled(state: u8) -> u8 {
    if state > WHOLE { 0 } else { state }
}

/// What is wrong with calling `name`, whose parameters are `params`
/// (without the first when `skip`: a method's `self`), with `args`.
pub fn misfit(name: &str, params: &[Param], skip: bool, args: &Args) -> Option<String> {
    let mut named: Vec<&Param> = params.iter().filter(|param| param.star == 0 && !param.only).collect();
    if skip {
        if named.is_empty() { return None; }
        named.remove(0);
    }
    let only = || params.iter().filter(|param| param.star == 0 && param.only);
    let star = |kind: u8| params.iter().any(|param| param.star == kind);
    let given = |param: &Param| args.keys.iter().any(|(key, _)| *key == param.name);
    if !star(2) {
        let fits = |key: &str| named.iter().any(|param| !param.positional && param.name == key) || only().any(|param| param.name == key);
        if let Some((key, _)) = args.keys.iter().find(|(key, _)| !fits(key)) {
            return Some(format!("{name}() got an unexpected keyword argument '{key}'"));
        }
    }
    // (A `*` of unknown length: how many are given is not known from here on.)
    if args.stars & 4 != 0 { return None; }
    let count = args.plain.len();
    if count > named.len() && !star(1) {
        let plural = if named.len() == 1 { "" } else { "s" };
        return Some(format!("{name}() takes {} positional argument{plural} but {count} were given", named.len()));
    }
    if let Some((key, _)) = args.keys.iter().find(|(key, _)| named.iter().take(count).any(|param| param.name == *key)) {
        return Some(format!("{name}() got multiple values for argument '{key}'"));
    }
    if args.stars & 2 == 0 {
        let missing: Vec<String> = named.iter().skip(count).copied().chain(only())
            .filter(|param| param.default.is_none() && !given(param)).map(|param| format!("'{}'", param.name)).collect();
        if !missing.is_empty() {
            let plural = if missing.len() == 1 { "" } else { "s" };
            return Some(format!("{name}() missing required argument{plural}: {}", missing.join(", ")));
        }
    }
    // A literal of the wrong type for an annotated parameter.
    let keyed = args.keys.iter().filter_map(|(key, got)| params.iter().find(|param| param.star == 0 && param.name == *key).map(|param| (param, *got)));
    for (param, got) in named.iter().copied().zip(args.plain.iter().copied()).chain(keyed) {
        let fits = match param.kind {
            0 => true,
            FLOAT => matches!(got, FLOAT | WHOLE | INT),
            INT => matches!(got, INT | WHOLE),
            want => got == want,
        };
        if got != 0 && !fits {
            return Some(format!("{name}() expected {} for '{}', got {}", TYPES[param.kind as usize], param.name, TYPES[got as usize]));
        }
    }
    None
}
