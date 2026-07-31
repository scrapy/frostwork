//! Differential bridge for the Python permutation harness (tools/diff_lxml.py).
//!
//! Protocol (line-oriented, no JSON parser needed on the Rust side):
//!   stdin  : flat case    = `ENC \t HEXHTML \t sel1 \t sel2 \t ...`  (ENC = charset label or empty
//!            for sniff; html hex-encoded so arbitrary bytes round-trip; selectors are plain)
//!            grouped case  = `G2 \t ENC \t HEXHTML \t k \t (CONTAINER \t m \t sub_1 … sub_m)×k`
//!            (k groups in ONE pass, so cross-group routing gets differential coverage)
//!   stdout : flat    -> a JSON array of value-columns (arrays of strings)
//!            grouped -> a JSON array of `k` groups, each an array of rows, each row an array of
//!                       sub-field value-columns  (`[group][row][sub][value]`)
//!
//! One long-running process streams the whole batch, so the Python driver (which owns lxml/Parsel)
//! can diff millions of (page × selector) pairs without a PyO3 build or a process-per-case.

use std::io::{self, BufRead, Write};

fn hex_decode(s: &str) -> Vec<u8> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len() / 2);
    let mut i = 0;
    while i + 1 < b.len() {
        let hi = (b[i] as char).to_digit(16);
        let lo = (b[i + 1] as char).to_digit(16);
        if let (Some(h), Some(l)) = (hi, lo) {
            out.push((h * 16 + l) as u8);
        }
        i += 2;
    }
    out
}

fn json_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Serialize a value column `["a","b"]` into `out`.
fn json_col(col: &[String], out: &mut String) {
    out.push('[');
    for (j, val) in col.iter().enumerate() {
        if j > 0 {
            out.push(',');
        }
        json_str(val, out);
    }
    out.push(']');
}

fn main() {
    // Opt-in (set by the parity harnesses, NOT by the budget-bomb selector fuzzer): treat a schema over
    // the fixed-width bitset budget as a harness error rather than silently answering empty columns.
    let budget_strict = std::env::var_os("FROSTWORK_DIFFER_BUDGET_STRICT").is_some();
    // A run cycles through a couple of fixed selector batches and varies only the HTML, so remember
    // every list already approved — `budget_usage` re-parses all of them, which otherwise cost ~30% of
    // the whole harness. A single-slot cache is not enough: consecutive lines alternate batches.
    let mut budget_ok: Vec<Vec<String>> = Vec::new();
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut o = io::BufWriter::new(stdout.lock());
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        if line.is_empty() {
            continue;
        }
        let mut parts = line.split('\t');
        let first = parts.next().unwrap_or("");
        let mut out = String::new();
        if first == "A" {
            // schema-audit probe used by selector fuzzing: A \t sel1 \t sel2 ... -> [bool, ...].
            // Keeping this in the Rust bridge means CI can make empty-result verdicts support-aware
            // before the Python extension is built.
            let sels: Vec<String> = parts.map(str::to_string).collect();
            let audit = frostwork::audit_schema(&sels, &[]);
            out.push('[');
            for (i, support) in audit.flat.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(if support.is_supported() { "true" } else { "false" });
            }
            out.push(']');
        } else if first == "G2" {
            // grouped: G2 \t ENC \t HEX \t k \t (CONTAINER \t m \t sub_1 … sub_m)×k
            let enc_label = parts.next().unwrap_or("");
            let hex = parts.next().unwrap_or("");
            let k: usize = parts.next().unwrap_or("0").parse().unwrap_or(0);
            let mut groups: Vec<frostwork::GroupQuery> = Vec::with_capacity(k);
            for _ in 0..k {
                let container = parts.next().unwrap_or("").to_string();
                let m: usize = parts.next().unwrap_or("0").parse().unwrap_or(0);
                let subfields: Vec<(String, String)> =
                    (0..m).map(|i| (i.to_string(), parts.next().unwrap_or("").to_string())).collect();
                groups.push(frostwork::GroupQuery { container, subfields });
            }
            let html = hex_decode(hex);
            let enc = if enc_label.is_empty() { None } else { Some(enc_label) };
            let (_flat, grouped) = frostwork::extract_grouped(&html, &[], &groups, enc);
            // [ group0_rows, group1_rows, ... ]  where group_rows = [ [ [values]*sub ]*row ]
            out.push('[');
            for (gi, rows) in grouped.iter().enumerate() {
                if gi > 0 {
                    out.push(',');
                }
                out.push('[');
                for (i, row) in rows.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    out.push('[');
                    for (j, col) in row.iter().enumerate() {
                        if j > 0 {
                            out.push(',');
                        }
                        json_col(col, &mut out);
                    }
                    out.push(']');
                }
                out.push(']');
            }
            out.push(']');
        } else {
            // flat: ENC \t HEX \t sel1 \t sel2 ...   (`first` is ENC)
            let enc_label = first;
            let hex = parts.next().unwrap_or("");
            let sels: Vec<String> = parts.map(|s| s.to_string()).collect();
            let html = hex_decode(hex);
            let enc = if enc_label.is_empty() { None } else { Some(enc_label) };
            // A schema over the fixed-width bitset budget yields deterministically EMPTY columns for
            // the members past the budget (see `budget_members_over_128_is_safe_empty`). That is safe
            // for a caller, and `sel_fuzz.py` deliberately feeds budget bombs to prove it — but it is
            // poison for a PARITY harness, where every over-budget column reads as a divergence against
            // lxml and buries any real one. So the parity harnesses opt in to failing loudly instead.
            if budget_strict && !budget_ok.contains(&sels) {
                let (members, sib) = frostwork::budget_usage(&sels, &[]);
                if members > frostwork::MAX_MEMBERS || sib > frostwork::MAX_SIB_BITS {
                    panic!(
                        "differ: schema over budget ({members} members / {sib} sibling bits vs limits \
                         {} / {}). Batch the selector basket — over-budget columns come back empty, \
                         not wrong, so a parity run would report them all as divergences.",
                        frostwork::MAX_MEMBERS,
                        frostwork::MAX_SIB_BITS
                    );
                }
                budget_ok.push(sels.clone());
            }
            let res = frostwork::extract(&html, &sels, enc);
            out.push('[');
            for (i, col) in res.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                json_col(col, &mut out);
            }
            out.push(']');
        }
        writeln!(o, "{out}").expect("write");
    }
}
