//! Rule-table MUTATION hook — compiled only under `--features mutate`, never in a shipped build.
//!
//! Why this exists. The tree-construction tables in [`crate::implied_close`] are the part of the engine
//! with the worst coverage economics: a cell is one `match` arm, no generated page necessarily exercises
//! it, and a wrong arm is silent. `tools/audit_tree_rules.py` answers "is every cell RIGHT?" by asking
//! lxml. This answers the other half — **"if a cell were wrong, would anything go red?"** — by flipping
//! one cell at a time and running the gates against the mutant.
//!
//! This finds blind spots without first guessing a missing example: it enumerates the modeled cells and
//! asks whether each change is observable. Rules the engine does not model as cells still need a wider
//! derivation before this sweep can reach them.
//!
//! Cost is why it is a runtime hook rather than a source rewrite: reading the mutation from the
//! environment means ONE build serves every mutant, instead of a rebuild-and-relink per cell.
//! Under the default feature set every function below is an `#[inline(always)]` identity, so the
//! production binary is byte-for-byte what it would be without the hook.
//!
//! Spec grammar (`FROSTWORK_MUTATE`), one mutation per run:
//! ```text
//!   close:<inc_name>,<open_name>   invert the EFFECTIVE "does this start tag close that open element?"
//!                                 answer for exactly that tag-name pair
//!   prio:<name>                   flip that name's end_priority between 0 and "out-ranks everything"
//!   void:<name>                   flip is_void for that name
//!   mode:<name>                   flip data_mode for that name (Normal <-> Rawtext)
//! ```
//!
//! `close:` hooks the EFFECTIVE answer rather than a table, which matters as soon as more than one table
//! feeds it. Two overlapping tables — say a hand-written tag-id one ORed with `start_closes` — MASK each
//! other: mutating either alone is invisible wherever the other closes the same pair, and 51 such mutants
//! survive every gate while the behaviour is in fact protected. Mutating the answer costs nothing extra
//! and stays honest whatever feeds it.
//!
//! Driven by `tools/mutate_rules.py`; see docs/TESTING.md.

#[cfg(not(feature = "mutate"))]
mod imp {
    use crate::tokenizer::DataMode;

    #[inline(always)]
    pub fn closes(_inc: &[u8], _top: &[u8], v: bool) -> bool {
        v
    }
    #[inline(always)]
    pub fn end_priority(_name: &str, v: u8) -> u8 {
        v
    }
    #[inline(always)]
    pub fn is_void(_name: &str, v: bool) -> bool {
        v
    }
    #[inline(always)]
    pub fn data_mode(_name: &str, v: DataMode) -> DataMode {
        v
    }
}

#[cfg(feature = "mutate")]
mod imp {
    use std::sync::OnceLock;

    use crate::tokenizer::DataMode;

    enum Spec {
        None,
        Close(String, String),
        Prio(String),
        Void(String),
        Mode(String),
    }

    fn spec() -> &'static Spec {
        static SPEC: OnceLock<Spec> = OnceLock::new();
        SPEC.get_or_init(|| {
            let raw = match std::env::var("FROSTWORK_MUTATE") {
                Ok(s) if !s.is_empty() => s,
                _ => return Spec::None,
            };
            let (kind, arg) = match raw.split_once(':') {
                Some(p) => p,
                None => panic!("FROSTWORK_MUTATE: expected `<kind>:<arg>`, got {raw:?}"),
            };
            match kind {
                "close" => {
                    let (a, b) = arg
                        .split_once(',')
                        .unwrap_or_else(|| panic!("FROSTWORK_MUTATE close: expected `inc,open`"));
                    Spec::Close(a.trim().to_ascii_lowercase(), b.trim().to_ascii_lowercase())
                }
                "prio" => Spec::Prio(arg.trim().to_ascii_lowercase()),
                "void" => Spec::Void(arg.trim().to_ascii_lowercase()),
                "mode" => Spec::Mode(arg.trim().to_ascii_lowercase()),
                other => panic!("FROSTWORK_MUTATE: unknown kind {other:?}"),
            }
        })
    }

    pub fn closes(inc: &[u8], top: &[u8], v: bool) -> bool {
        match spec() {
            Spec::Close(a, b)
                if inc.eq_ignore_ascii_case(a.as_bytes()) && top.eq_ignore_ascii_case(b.as_bytes()) =>
            {
                !v
            }
            _ => v,
        }
    }

    /// Move a name to the other end of the end-tag priority order. Both directions are real mistakes:
    /// a name that wrongly out-ranks everything makes every stray end tag near it disappear, and one
    /// that wrongly out-ranks nothing lets a stray `</div>` unwind a whole table. One mutant per NAME is
    /// enough here, unlike `close:`, because a single table feeds this answer — there is no second rule
    /// to mask the flip.
    pub fn end_priority(name: &str, v: u8) -> u8 {
        match spec() {
            Spec::Prio(n) if n == name => if v == 0 { u8::MAX } else { 0 },
            _ => v,
        }
    }

    pub fn is_void(name: &str, v: bool) -> bool {
        match spec() {
            Spec::Void(n) if n == name => !v,
            _ => v,
        }
    }

    /// Flip an element's data mode between "ordinary markup" and "raw text". Both directions are real
    /// mistakes: treating `<iframe>` as markup fabricates the elements inside it, and treating
    /// `<listing>` as raw text loses every element inside it.
    pub fn data_mode(name: &str, v: DataMode) -> DataMode {
        match spec() {
            Spec::Mode(n) if n == name => match v {
                DataMode::Normal => DataMode::Rawtext,
                _ => DataMode::Normal,
            },
            _ => v,
        }
    }
}

pub use imp::*;
