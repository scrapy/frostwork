//! Bounded state machines used by deferred-close predicates.
//!
//! Keeping these independent of stack mutation makes the memory invariant explicit: predicate input
//! is streamed, while only prospective terminal output remains on the open element.

use crate::selector::{TextAxis, TextOp, TextPred};

pub(super) struct TextMatchState {
    pub(super) entry: u32,
    pub(super) accum: TextAccum,
}

pub(super) enum TextAccum {
    StringEq { seen: usize, possible: bool },
    StringContains { tail: Vec<u8>, found: bool },
    DirectEq { found: bool },
    DirectContains { saw_first: bool, found: bool },
}

impl TextMatchState {
    pub(super) fn new(entry: u32, pred: &TextPred) -> Self {
        let accum = match (pred.axis, pred.op) {
            (TextAxis::StringValue, TextOp::Eq) => TextAccum::StringEq { seen: 0, possible: true },
            (TextAxis::StringValue, TextOp::Contains) => {
                TextAccum::StringContains { tail: Vec::new(), found: pred.needle.is_empty() }
            }
            (TextAxis::DirectText, TextOp::Eq) => TextAccum::DirectEq { found: false },
            (TextAxis::DirectText, TextOp::Contains) => TextAccum::DirectContains {
                saw_first: false,
                // XPath converts an empty node-set to ""; contains("", "") is true.
                found: pred.needle.is_empty(),
            },
        };
        Self { entry, accum }
    }

    pub(super) fn update(&mut self, pred: &TextPred, value: &str, direct: bool) {
        match &mut self.accum {
            TextAccum::StringEq { seen, possible } => {
                if !*possible {
                    return;
                }
                let end = seen.saturating_add(value.len());
                if end > pred.needle.len()
                    || pred.needle.as_bytes().get(*seen..end) != Some(value.as_bytes())
                {
                    *possible = false;
                }
                *seen = end;
            }
            TextAccum::StringContains { tail, found } => {
                if *found {
                    return;
                }
                let needle = pred.needle.as_bytes();
                tail.extend_from_slice(value.as_bytes());
                if tail.windows(needle.len()).any(|w| w == needle) {
                    *found = true;
                    tail.clear();
                } else {
                    let keep = needle.len().saturating_sub(1);
                    if tail.len() > keep {
                        tail.drain(..tail.len() - keep);
                    }
                }
            }
            TextAccum::DirectEq { found } if direct => *found |= value == pred.needle,
            TextAccum::DirectContains { saw_first, found } if direct && !*saw_first => {
                *saw_first = true;
                *found |= value.contains(&pred.needle);
            }
            _ => {}
        }
    }

    pub(super) fn holds(&self, pred: &TextPred) -> bool {
        match &self.accum {
            TextAccum::StringEq { seen, possible } => *possible && *seen == pred.needle.len(),
            TextAccum::StringContains { found, .. }
            | TextAccum::DirectEq { found }
            | TextAccum::DirectContains { found, .. } => *found,
        }
    }
}
