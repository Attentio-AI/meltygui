//! Bindings, references and calls: which names a text binds, in which
//! scope and how; where it uses names (with their dotted chains); what it
//! calls and with which shape of arguments.
//!
//! `Binder::line` reads one line's tokens (`scan.rs`) the way Python's own
//! binding rules do, statement by statement and never failing: a `def` or
//! `class` name, parameters, assignment targets, imports, `for`, `with` and
//! `except` targets, comprehension targets, `global` and `nonlocal`. It also
//! counts the open brackets: a line that starts inside one continues a
//! statement and neither ends a scope nor opens one (`facts.rs`).
//! To bind another construct, add its rule to `statement`.
//!
//! What a chunk's lines bind does not depend on the lines around it unless a
//! statement is open where the chunk starts, so it is kept on the chunk
//! (`Bound`, through `Chunk::bound`): a reading after an edit binds the
//! edited chunk again and nothing else. The scope a name is bound in is
//! only known to the pass over all lines, so a `Bound` names it by role:
//! the line's own scope, the definition the line opens, the function whose
//! header is being read.
use crate::facts::{hash, mix};
use crate::scan::{Scan, Strings, Tok, BLANK, CLASS as CLASS_LINE, COMMENT, DEF as DEF_LINE, KEYWORD, NAME, NOTE, NUMBER, OP, OPS, STRING};

/// Binding kinds, in the order of `KINDS`.
pub const DEF: u8 = 0;
pub const CLASS: u8 = 1;
pub const PARAM: u8 = 2;
pub const ASSIGN: u8 = 3;
pub const IMPORT: u8 = 4;
pub const FOR: u8 = 5;
pub const WITH: u8 = 6;
pub const EXCEPT: u8 = 7;
pub const COMP: u8 = 8;
pub const GLOBAL: u8 = 9;
pub const NONLOCAL: u8 = 10;
/// A lambda's parameter: bound for name lookup in the scope the lambda is
/// written in (wider than Python has it: a lookup errs on finding the name).
pub const LAMBDA: u8 = 11;
/// A name a `del` statement names: the scope has it (it was bound to be deleted).
pub const DEL: u8 = 12;
pub const KINDS: [&str; 13] =
    ["def", "class", "param", "assign", "import", "for", "with", "except", "comp", "global", "nonlocal", "lambda", "del"];

#[derive(Clone)]
pub struct Binding {
    /// Index of the definition whose scope it is bound in, or -1 for the
    /// module. In a `Bound`: `LINE`, `OPENED` or `HEADER`.
    pub scope: i32,
    pub kind: u8,
    pub line: u32,
    /// Where the name is: chunk, byte in it, bytes; and its hash.
    pub chunk: u32,
    pub start: u32,
    pub len: u16,
    pub hash: u64,
}

/// A name used: the dotted chain that starts at byte `start` of the chunk
/// (`len` bytes) on chunk line `line`; `store` when the statement binds it.
pub struct Ref {
    pub line: u32,
    pub start: u32,
    pub len: u16,
    pub store: bool,
    /// It stands in an annotation.
    pub note: bool,
}

/// A call of a dotted chain: where the chain is, and the byte of its
/// opening bracket (the arguments are read from there when asked for:
/// `Facts::calls`).
pub struct Call {
    pub line: u32,
    pub start: u32,
    pub len: u16,
    pub paren: u32,
    /// It is made in a lambda's body.
    pub lambda: bool,
}

/// A bracket that cannot be right: `what` is `NEVER` closed, `UNMATCHED`
/// (a closing one with none open) or `OTHER` (it closes `other`, opened on
/// line `from`). Lines count from the chunk's first.
pub struct Flaw {
    pub line: i32,
    pub what: u8,
    pub bracket: u8,
    pub other: u8,
    pub from: i32,
}

pub const NEVER: u8 = 0;
pub const UNMATCHED: u8 = 1;
pub const OTHER: u8 = 2;

/// Scopes by role: the line's own, the definition the line opens, the
/// function whose header (the last `def` line) the line belongs to.
pub const LINE: i32 = -2;
pub const OPENED: i32 = -3;
pub const HEADER: i32 = -4;

/// What the open brackets of a statement hold.
const PARAMS: u8 = 1;
const IMPORTS: u8 = 2;
/// Targets going on over the next lines: of a `for` until its `in`, of an
/// assignment until its bracket closes (kept only if an `=` follows).
const TARGETS: u8 = 3;
/// A `with` whose items go on over the next lines: each `as` binds.
const ITEMS: u8 = 4;
/// A `case` whose bracketed pattern goes on over the next lines (kept only
/// if a `:` follows where it closes).
const CASE: u8 = 5;

#[derive(Default)]
pub struct Binder {
    /// Open brackets at the start of the next line, and whether the line
    /// before ended in a backslash.
    pub depth: i32,
    slash: bool,
    mode: u8,
    /// The bracket depth a `lambda` among default values was met at.
    lambda: Option<i32>,
    /// The kind of the targets going on, and where they start in `found`.
    kind: u8,
    pending: usize,
    pub found: Vec<Binding>,
    /// For `uses`: the open brackets; the statement is an import (its names
    /// are no references); the bracket count a `lambda`'s parameters are at.
    level: usize,
    plain: bool,
    params: Option<usize>,
    /// The bracket count the outermost lambda being read stands at.
    body: Option<usize>,
    /// The statement is a `def` whose header is being read; an annotation is
    /// being read; the byte of the `:` that starts an annotated name's.
    header: bool,
    noting: bool,
    annotation: Option<u32>,
    /// A `from x import *` was read.
    pub star: bool,
    /// The line before ended in a `.`: the name that follows is a member.
    dot: bool,
    /// The statement is a `def` or `class` header or a decorator (`Bound::heads`).
    head: bool,
    /// The brackets open, innermost last: (the bracket, its line counted
    /// from the chunk's first: below zero in a chunk above).
    pub stack: Vec<(u8, i32)>,
    pub flaws: Vec<Flaw>,
    /// The statement is a `match` (1) or `case` (2) header: its first word
    /// is no name, and neither is a `_` in a pattern.
    soft: u8,
    pub refs: Vec<Ref>,
    pub calls: Vec<Call>,
}

/// A line of code and where it is, for the rules below.
struct Line<'a> {
    text: &'a str,
    line: u32,
}

impl Line<'_> {
    fn word(&self, tok: Tok) -> &str {
        &self.text[tok.start as usize..tok.start as usize + tok.len as usize]
    }

    /// The token is the one character `op`.
    fn is(&self, tok: Tok, op: u8) -> bool {
        tok.kind == OP && self.text.as_bytes()[tok.start as usize] == op
    }

    fn among(&self, tok: Tok, ops: &[u8]) -> bool {
        tok.kind == OP && ops.contains(&self.text.as_bytes()[tok.start as usize])
    }

    fn keyword(&self, tok: Tok, word: &str) -> bool {
        tok.kind == KEYWORD && self.word(tok) == word
    }

    /// Index of the first token from `from` outside brackets that `test` accepts.
    fn find(&self, code: &[Tok], from: usize, test: impl Fn(Tok) -> bool) -> Option<usize> {
        let mut depth = 0;
        for (at, &tok) in code.iter().enumerate().skip(from) {
            if depth == 0 && test(tok) { return Some(at); }
            if self.among(tok, b"([{") { depth += 1; } else if self.among(tok, b")]}") { depth -= 1; }
        }
        None
    }
}

impl Binder {
    fn bind(&mut self, at: &Line, tok: Tok, kind: u8, scope: i32) {
        self.found.push(Binding { scope, kind, line: at.line, chunk: 0, start: tok.start, len: tok.len, hash: hash(at.word(tok)) });
    }

    /// Whether the line starts a statement that cannot be inside brackets:
    /// an unclosed bracket above it (code being typed) ends there.
    fn fresh(at: &Line, tok: Tok) -> bool {
        // (Not `@`: inside brackets a line may start with the operator.)
        tok.kind == KEYWORD && matches!(at.word(tok), "def" | "class" | "return" | "import" | "while" | "try" | "except"
            | "finally" | "with" | "raise" | "assert" | "pass" | "break" | "continue" | "global" | "nonlocal" | "del" | "elif")
    }

    /// Whether a line with these tokens continues the statement above it.
    pub fn continues(&self, text: &str, toks: &[Tok]) -> bool {
        // (An empty line inside brackets is still inside them.)
        if toks.is_empty() { return self.depth > 0; }
        (self.depth > 0 || self.slash) && toks.first().is_some_and(|&tok| !Binder::fresh(&Line { text, line: 0 }, tok))
    }

    /// The binder a chunk of `lines` lines left after its last, to go on with in the next chunk.
    pub fn resume(&self, lines: usize) -> Binder {
        Binder { stack: self.stack.iter().map(|&(bracket, line)| (bracket, line - lines as i32)).collect(), depth: self.depth, slash: self.slash, mode: self.mode, lambda: self.lambda, kind: self.kind, pending: 0,
                 level: self.level, plain: self.plain, params: self.params, body: self.body, header: self.header, noting: self.noting,
                 annotation: None, dot: self.dot, soft: self.soft, head: self.head, ..Binder::default() }
    }

    /// No statement is open: the lines below bind the same whatever is above.
    fn clean(&self) -> bool {
        self.depth == 0 && !self.slash && self.mode == 0
    }

    /// The statement ends here: a bracket still open in it is never closed.
    pub fn cut(&mut self) {
        if let Some(&(bracket, line)) = self.stack.last() {
            self.flaws.push(Flaw { line, what: NEVER, bracket, other: 0, from: 0 });
        }
        self.stack.clear();
    }

    /// Read one line (line `line` of its chunk).
    pub fn line(&mut self, text: &str, toks: &[Tok], line: u32) {
        let (at, scope) = (Line { text, line }, LINE);
        let code = match toks.last() {
            Some(last) if last.kind == NOTE => &toks[..toks.len() - 1],
            _ => toks,
        };
        if code.is_empty() { return; }
        let continued = self.continues(text, code);
        let before = self.found.len();
        self.slash = code.last().is_some_and(|&tok| at.is(tok, b'\\'));
        if !continued {
            // A statement starts with a bracket still open above it: that one is never closed.
            self.cut();
            (self.depth, self.mode, self.lambda, self.plain, self.params, self.level) = (0, 0, None, false, None, 0);
            (self.header, self.noting, self.annotation, self.dot, self.body, self.soft) = (false, false, None, false, None, 0);
            self.statement(&at, code, scope);
        }
        // Brackets, and what binds wherever it stands: parameters and import
        // lists over several lines, `name :=`, comprehension targets.
        for (index, &tok) in code.iter().enumerate() {
            let before = index.checked_sub(1).map(|index| code[index]);
            if at.among(tok, b"([{") {
                self.depth += 1;
                self.stack.push((text.as_bytes()[tok.start as usize], line as i32));
            } else if at.among(tok, b")]}") {
                self.depth = (self.depth - 1).max(0);
                let bracket = text.as_bytes()[tok.start as usize];
                match self.stack.pop() {
                    None => self.flaws.push(Flaw { line: line as i32, what: UNMATCHED, bracket, other: 0, from: 0 }),
                    Some((open, from)) if open != [b'(', b'[', b'{'][b")]}".iter().position(|&close| close == bracket).unwrap_or(0)] => {
                        self.flaws.push(Flaw { line: line as i32, what: OTHER, bracket, other: open, from });
                    }
                    _ => {}
                }
                if self.depth == 0 {
                    // A bracketed target list closed: it was one if an `=` follows.
                    if self.mode == TARGETS && self.kind == ASSIGN && !code.get(index + 1).is_some_and(|&after| at.is(after, b'=')) {
                        self.found.truncate(self.pending);
                    }
                    if self.mode < TARGETS || (self.mode == TARGETS && self.kind == ASSIGN) { self.mode = 0; }
                    if self.mode == CASE {
                        // The pattern closed: it was one if its `:`, a guard or an `as name` follows.
                        match code.get(index + 1) {
                            Some(&after) if at.keyword(after, "as") => {
                                if let Some(&name) = code.get(index + 2).filter(|tok| tok.kind == NAME) { self.bind(&at, name, ASSIGN, scope); }
                            }
                            Some(&after) if at.is(after, b':') || at.keyword(after, "if") => {}
                            _ => self.found.truncate(self.pending),
                        }
                        self.mode = 0;
                    }
                }
            } else if self.mode == ITEMS && continued && at.keyword(tok, "as") {
                self.given(&at, code, index, WITH, scope);
            } else if self.mode == CASE && continued {
                if Binder::captured(&at, code, index) { self.bind(&at, tok, ASSIGN, scope); }
            } else if self.mode == TARGETS && at.keyword(tok, "in") {
                self.mode = 0;
            } else if self.mode == TARGETS && continued && tok.kind == NAME {
                if before.map_or(true, |before| at.among(before, b",([*")) && !code.get(index + 1).is_some_and(|&after| at.among(after, b".([")) {
                    self.bind(&at, tok, self.kind, scope);
                }
            } else if at.keyword(tok, "lambda") {
                self.lambda.get_or_insert(self.depth);
            } else if at.is(tok, b':') && self.lambda == Some(self.depth) {
                self.lambda = None;
            } else if at.keyword(tok, "for") && self.depth > 0 {
                let end = code[index..].iter().position(|&tok| at.keyword(tok, "in")).map_or(code.len(), |end| index + end);
                self.targets(&at, &code[index + 1..end], COMP, scope);
            } else if tok.kind == NAME {
                let after = code.get(index + 1);
                if after.is_some_and(|&after| after.kind == OPS && at.word(after) == ":=") {
                    self.bind(&at, tok, ASSIGN, scope);
                } else if self.mode == PARAMS && self.depth == 1 && self.lambda.is_none()
                    && before.map_or(true, |before| at.among(before, b"(,*") || (before.kind == OPS && at.word(before) == "**")) {
                    self.bind(&at, tok, PARAM, HEADER);
                } else if self.mode == IMPORTS && self.depth > 0 && !after.is_some_and(|&after| at.keyword(after, "as")) {
                    self.bind(&at, tok, IMPORT, scope);
                }
            }
        }
        // In the order they stand on the line.
        let before = before.min(self.found.len());
        self.found[before..].sort_by_key(|found| found.start);
        self.uses(&at, code, before, continued);
    }

    /// The names the line uses and the calls it makes.
    fn uses(&mut self, at: &Line, code: &[Tok], bound: usize, continued: bool) {
        // A chain being read: (its first byte, it counts as a reference), and the byte it ends at.
        let mut chain: Option<(u32, bool)> = None;
        let mut end = 0;
        for (index, &tok) in code.iter().enumerate() {
            let before = index.checked_sub(1).map(|index| code[index]);
            let after = code.get(index + 1).copied();
            let lambda = self.params == Some(self.level);
            // A lambda's body ends at the `,` after it (or where its bracket closes).
            if at.is(tok, b',') && self.body == Some(self.level) && self.params.is_none() { self.body = None; }
            if at.among(tok, b"([{") {
                if at.is(tok, b'(') && before.is_some_and(|before| before.kind == NAME) && chain.is_some_and(|chain| chain.1) {
                    let start = chain.unwrap().0;
                    self.calls.push(Call { line: at.line, start, len: (end - start) as u16, paren: tok.start, lambda: self.body.is_some() });
                }
                self.level += 1;
            } else if at.among(tok, b")]}") {
                self.level = self.level.saturating_sub(1);
                self.noting &= !(self.header && self.level == 0);
                self.body = self.body.filter(|&body| body <= self.level);
            } else if at.keyword(tok, "lambda") {
                self.params.get_or_insert(self.level);
                self.body.get_or_insert(self.level);

            } else if at.is(tok, b':') && lambda {
                self.params = None;
            } else if self.header && self.level <= 1 && !lambda && (at.among(tok, b":,=") || tok.kind == OPS) {
                // A parameter's annotation runs from its `:` to the `,` or `=`; the result's from `->` to the header's `:`.
                self.noting = if self.level == 1 { at.is(tok, b':') } else { at.word(tok) == "->" };
                self.header &= !(self.level == 0 && at.is(tok, b':'));
            } else if self.annotation == Some(tok.start) {
                self.noting = true;
            } else if !self.header && self.level == 0 && at.is(tok, b'=') {
                self.noting = false;
            }
            if tok.kind != NAME {
                if !at.is(tok, b'.') { chain = None; }
                continue;
            }
            end = tok.start + tok.len as u32;
            if before.map_or(self.dot, |before| at.is(before, b'.')) {
                // A member: it lengthens the chain it is on.
                if let (Some((start, true)), Some(found)) = (chain, self.refs.last_mut()) {
                    if found.start == start && found.line == at.line { found.len = (end - start) as u16; }
                }
                continue;
            }
            let binding = self.found[bound..].iter().find(|found| found.start == tok.start && found.line == at.line).map(|found| found.kind);
            let key = after.is_some_and(|after| at.is(after, b'=')) && self.level > 0 && before.map_or(continued, |before| at.among(before, b"(,"));
            let parameter = lambda && before.is_some_and(|before| at.keyword(before, "lambda") || at.among(before, b",*") || before.kind == OPS);
            let soft = (index == 0 && !continued && self.soft != 0) || (self.soft == 2 && at.word(tok) == "_");
            let counts = !self.plain && !key && !parameter && !soft
                && !matches!(binding, Some(DEF | CLASS | PARAM | IMPORT | EXCEPT | GLOBAL | NONLOCAL));
            chain = Some((tok.start, counts));
            if counts {
                self.refs.push(Ref { line: at.line, start: tok.start, len: tok.len, store: binding.is_some(), note: self.noting });
            } else if parameter {
                self.bind(at, tok, LAMBDA, LINE);
            }
        }
        self.dot = code.last().is_some_and(|&tok| at.is(tok, b'.'));
        // An annotation ends with its line unless a bracket carries it on.
        if self.level == 0 && !self.header { (self.noting, self.annotation) = (false, None); }
    }

    /// Whether the name at `code[index]` of a `case` pattern is captured:
    /// not a class, a keyword of one, a dotted value or `_`.
    fn captured(at: &Line, code: &[Tok], index: usize) -> bool {
        code[index].kind == NAME && at.word(code[index]) != "_"
            && !index.checked_sub(1).is_some_and(|before| at.is(code[before], b'.'))
            && !code.get(index + 1).is_some_and(|&after| at.among(after, b".(="))
    }

    /// Whether the statement is a `match` or `case` header (soft keywords:
    /// `case = 1` and `match(x)` are none). A `case` binds the names its
    /// pattern captures, up to its guard.
    fn clause(&mut self, at: &Line, code: &[Tok], scope: i32) -> bool {
        if !code.get(1).is_some_and(|&tok| !at.among(tok, b"=.,:") && !(tok.kind == OPS && at.word(tok).ends_with('='))) { return false; }
        let colon = at.find(code, 1, |tok| at.is(tok, b':'));
        let open = code.iter().map(|&tok| at.among(tok, b"([{") as i32 - at.among(tok, b")]}") as i32).sum::<i32>() > 0;
        let pattern = match (at.word(code[0]), colon) {
            ("match", colon) => {
                self.soft = (colon == Some(code.len() - 1)) as u8;
                return self.soft != 0;
            }
            (_, Some(colon)) => &code[1..colon],
            // A pattern in brackets that goes on over the next lines.
            (_, None) if open && at.among(code[1], b"([{") => {
                (self.mode, self.pending) = (CASE, self.found.len());
                &code[1..]
            }
            _ => return false,
        };
        self.soft = 2;
        let guard = at.find(pattern, 0, |tok| at.keyword(tok, "if")).unwrap_or(pattern.len());
        for index in (0..guard).filter(|&index| Binder::captured(at, pattern, index)) { self.bind(at, pattern[index], ASSIGN, scope); }
        // The statement on the same line.
        if let Some(colon) = colon.filter(|colon| colon + 1 < code.len()) { self.statement(at, &code[colon + 1..], scope); }
        true
    }

    /// The bindings of a statement that starts at `code[0]`.
    fn statement(&mut self, at: &Line, code: &[Tok], scope: i32) {
        if let Some(end) = at.find(code, 0, |tok| at.is(tok, b';')) {
            // Statements on one line.
            if end > 0 { self.statement(at, &code[..end], scope); }
            if end + 1 < code.len() { self.statement(at, &code[end + 1..], scope); }
            return;
        }
        let first = (at.keyword(code[0], "async") && code.len() > 1) as usize;
        let head = code[first];
        let named = |index: usize| code.get(index).copied().filter(|tok| tok.kind == NAME);
        // After a compound statement's header, the statement on the same line.
        let mut body = scope;
        match if head.kind == KEYWORD { at.word(head) } else { "" } {
            "def" | "class" => {
                if let Some(name) = named(first + 1) {
                    self.bind(at, name, if at.word(head) == "def" { DEF } else { CLASS }, scope);
                }
                if at.word(head) == "def" { (self.mode, self.header) = (PARAMS, true); }
                body = OPENED;
            }
            "import" => {
                self.plain = true;
                self.imports(at, &code[first + 1..], scope);
            }
            "from" => {
                self.plain = true;
                self.star |= code.iter().any(|&tok| at.is(tok, b'*'));
                if let Some(import) = code.iter().position(|&tok| at.keyword(tok, "import")) {
                    if code.get(import + 1).is_some_and(|&tok| at.is(tok, b'(')) {
                        self.mode = IMPORTS;
                    } else {
                        self.imports(at, &code[import + 1..], scope);
                    }
                }
                return;
            }
            "global" | "nonlocal" => {
                let kind = if at.word(head) == "global" { GLOBAL } else { NONLOCAL };
                for &tok in code.iter().filter(|tok| tok.kind == NAME) { self.bind(at, tok, kind, scope); }
                return;
            }
            "for" => {
                let end = at.find(code, first, |tok| at.keyword(tok, "in"));
                if end.is_none() { (self.mode, self.kind) = (TARGETS, FOR); }
                self.targets(at, &code[first + 1..end.unwrap_or(code.len())], FOR, scope);
            }
            "with" | "except" => {
                let kind = if at.word(head) == "with" { WITH } else { EXCEPT };
                for index in (0..code.len()).filter(|&index| at.keyword(code[index], "as")) {
                    self.given(at, code, index, kind, scope);
                }
                if kind == WITH && at.find(code, first, |tok| at.is(tok, b':')).is_none() { self.mode = ITEMS; }
            }
            "del" => {
                self.targets(at, &code[first + 1..], DEL, scope);
                return;
            }
            "if" | "elif" | "else" | "while" | "try" | "finally" => {}
            "" if head.kind == NAME && matches!(at.word(head), "match" | "case") && self.clause(at, code, scope) => return,
            "" if !at.is(head, b'@') => {
                // An assignment: targets before the last `=`, or the one name
                // of an augmented or annotated one. (A lambda's defaults are not one.)
                let code = &code[..at.find(code, 0, |tok| at.keyword(tok, "lambda")).unwrap_or(code.len())];
                if code.is_empty() { return; }
                // (An annotated statement's target is a name or a dotted one: `self.name: type = value`.)
                let target = code.iter().position(|&tok| !(tok.kind == NAME || at.is(tok, b'.'))).filter(|&end| code[0].kind == NAME && at.is(code[end], b':'));
                if let Some(end) = target { self.annotation = Some(code[end].start); }
                let plain = |tok: Tok| at.is(tok, b'=');
                let mut last = at.find(code, 0, plain);
                while let Some(next) = last.and_then(|last| at.find(code, last + 1, plain)) { last = Some(next); }
                let augmented = |tok: Tok| tok.kind == OPS && at.word(tok).ends_with('=') && !matches!(at.word(tok), "==" | "!=" | "<=" | ">=" | ":=");
                if let Some(last) = last {
                    self.targets(at, &code[..last], ASSIGN, scope);
                } else if code.len() > 1 && code[0].kind == NAME && (augmented(code[1]) || at.is(code[1], b':')) {
                    self.bind(at, code[0], ASSIGN, scope);
                } else if at.among(code[0], b"([") && code.iter().map(|&tok| at.among(tok, b"([{") as i32 - at.among(tok, b")]}") as i32).sum::<i32>() > 0 {
                    // A bracketed target list that goes on below.
                    (self.mode, self.kind, self.pending) = (TARGETS, ASSIGN, self.found.len());
                    self.targets(at, code, ASSIGN, scope);
                }
                return;
            }
            _ => return,
        }
        if let Some(colon) = at.find(code, first, |tok| at.is(tok, b':')) {
            if colon + 1 < code.len() { self.statement(at, &code[colon + 1..], body); }
        }
    }

    /// What the `as` at `code[index]` names: a name, or the names in brackets.
    fn given(&mut self, at: &Line, code: &[Tok], index: usize, kind: u8, scope: i32) {
        let end = match code.get(index + 1) {
            Some(&open) if at.is(open, b'(') => code[index..].iter().position(|&tok| at.is(tok, b')')).map_or(code.len(), |end| index + end),
            _ => (index + 2).min(code.len()),
        };
        self.targets(at, &code[index + 1..end], kind, scope);
    }

    /// `a.b as c, d`: each item binds the name after `as`, else its first name.
    fn imports(&mut self, at: &Line, code: &[Tok], scope: i32) {
        for item in code.split(|&tok| at.is(tok, b',')) {
            let renamed = item.iter().position(|&tok| at.keyword(tok, "as")).and_then(|index| item.get(index + 1));
            if let Some(&name) = renamed.or(item.first()).filter(|tok| tok.kind == NAME) {
                self.bind(at, name, IMPORT, scope);
            }
        }
    }

    /// The names a target list stores to: not an attribute, not a call or
    /// subscript, not inside one.
    fn targets(&mut self, at: &Line, code: &[Tok], kind: u8, scope: i32) {
        let mut inside: Vec<bool> = vec![];
        for (index, &tok) in code.iter().enumerate() {
            let before = index.checked_sub(1).map(|index| code[index]);
            if at.among(tok, b"([{") {
                // A bracket after a value is a call or a subscript; a brace is never a target.
                inside.push(at.is(tok, b'{') || before.is_some_and(|before| matches!(before.kind, NAME | STRING | NUMBER) || at.among(before, b")]")));
            } else if at.among(tok, b")]}") {
                inside.pop();
            } else if tok.kind == NAME && !inside.contains(&true)
                && before.map_or(true, |before| at.among(before, b",([*="))
                && !code.get(index + 1).is_some_and(|&after| at.among(after, b".([")) {
                self.bind(at, tok, kind, scope);
            }
        }
    }
}

/// What the lines of one chunk bind, and which of them continue a statement.
pub struct Bound {
    /// Bindings by chunk line, their scopes by role.
    pub binds: Vec<Binding>,
    /// The names used and the calls made, by chunk line.
    pub refs: Vec<Ref>,
    pub calls: Vec<Call>,
    pub continued: Vec<bool>,
    /// A `from x import *` is among the lines.
    pub star: bool,
    /// The lines calls are checked against, hashed: those of `def` and
    /// `class` headers and of decorators.
    pub heads: u64,
    /// The brackets that cannot be right.
    pub flaws: Vec<Flaw>,
    /// The binder after the last line, when a statement is still open there.
    pub open: Option<Binder>,
}

impl Bound {
    /// Read a chunk's lines with `binder`, as the statement open above them left it.
    pub fn read(text: &str, scan: &Scan, strings: &Strings, mut binder: Binder) -> Bound {
        let mut continued = Vec::with_capacity(scan.lines.len());
        let mut heads = 0;
        for line in 0..scan.lines.len() {
            let (state, toks) = (strings.lines[line], strings.on(line));
            // A line that began inside a string is no statement; code after the string's
            // end still closes brackets, and the fields of an f-string still use names.
            let goes = state & 1 == 0 && binder.continues(text, toks);
            continued.push(goes);
            if !goes && state & 1 == 0 && scan.lines[line].flags & (BLANK | COMMENT) == 0 {
                let head = scan.lines[line].flags & (DEF_LINE | CLASS_LINE) != 0 || toks.first().is_some_and(|tok| tok.kind == OP && text.as_bytes()[tok.start as usize] == b'@');
                // (Another statement after a decorator: it decorates nothing.)
                if binder.head && !head { heads = mix(heads, 1); }
                binder.head = head;
            }
            if binder.head {
                heads = toks.iter().fold(heads, |heads, tok| mix(heads, hash(&text[tok.start as usize..tok.start as usize + tok.len as usize])));
            }
            if state & 1 == 0 || state & 2 != 0 || toks.len() > 1 { binder.line(text, toks, line as u32); }
        }
        let (binds, refs, calls) = (std::mem::take(&mut binder.found), std::mem::take(&mut binder.refs), std::mem::take(&mut binder.calls));
        let flaws = std::mem::take(&mut binder.flaws);
        Bound { binds, refs, calls, continued, star: binder.star, heads, flaws, open: (!binder.clean()).then_some(binder) }
    }
}
