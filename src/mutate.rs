//! Rule-table MUTATION hook — compiled only under `--features mutate`, never in a shipped build.
//!
//! Why this exists. The tree-construction tables in [`crate::implied_close`] are the part of the engine
//! with the worst coverage economics: a cell is one `match` arm, no generated page necessarily exercises
//! it, and a wrong arm is silent. `tools/audit_tree_rules.py` answers "is every cell RIGHT?" by asking
//! lxml. This answers the other half — **"if a cell were wrong, would anything go red?"** — by flipping
//! one cell at a time and running the gates against the mutant.
//!
//! That is the only check in the repo that finds blind spots without someone first guessing where they
//! are. Every other "did we test this?" question here was answered by a human noticing an absence: the
//! `dd`/`dt` arm, the missing `colgroup` rule, escapes in selectors. This one enumerates.
//!
//! Cost is why it is a runtime hook rather than a source rewrite: reading the mutation from the
//! environment means ONE build serves all ~400 mutants, instead of ~400 rebuild-and-relink cycles.
//! Under the default feature set every function below is an `#[inline(always)]` identity, so the
//! production binary is byte-for-byte what it would be without the hook.
//!
//! Spec grammar (`FROSTWORK_MUTATE`), one mutation per run:
//! ```text
//!   close:<inc_name>,<open_name>   invert the EFFECTIVE "does this start tag close that open element?"
//!                                 answer for exactly that tag-name pair
//!   scope:<tag_id>                flip is_table_scoped for that id
//!   void:<name>                   flip is_void for that name
//! ```
//!
//! `close:` deliberately hooks the COMPOSITE decision rather than one table. Two tables feed it —
//! `implies_close_id` (tag ids) and `start_closes` (libxml2's finer name-pair list) — and they overlap,
//! so mutating either one alone is MASKED wherever the other closes the same pair: 51 such mutants
//! survived every gate while the behaviour was in fact protected. Mutating the answer instead of an
//! implementation detail makes every cell observable and needs no equivalence bookkeeping.
//!
//! Driven by `tools/mutate_rules.py`; see docs/TESTING.md.

#[cfg(not(feature = "mutate"))]
mod imp {
    #[inline(always)]
    pub fn closes(_inc: &[u8], _top: &[u8], v: bool) -> bool {
        v
    }
    #[inline(always)]
    pub fn scope(_tid: u8, v: bool) -> bool {
        v
    }
    #[inline(always)]
    pub fn is_void(_name: &str, v: bool) -> bool {
        v
    }
}

#[cfg(feature = "mutate")]
mod imp {
    use std::sync::OnceLock;

    enum Spec {
        None,
        Close(String, String),
        Scope(u8),
        Void(String),
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
                "scope" => Spec::Scope(arg.trim().parse().unwrap()),
                "void" => Spec::Void(arg.trim().to_ascii_lowercase()),
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

    pub fn scope(tid: u8, v: bool) -> bool {
        match spec() {
            Spec::Scope(t) if *t == tid => !v,
            _ => v,
        }
    }

    pub fn is_void(name: &str, v: bool) -> bool {
        match spec() {
            Spec::Void(n) if n == name => !v,
            _ => v,
        }
    }

}

pub use imp::*;
