//! Engine-only throughput (no subprocess/IO/JSON overhead). Reads selectors from stdin (one per
//! line, blank lines ignored) so shell quoting can't corrupt `>`/`[]`/`()`.
//! Usage: `bench HTML_FILE < selectors.txt` (empty stdin = 0 selectors = pure scan).
//!   Flat mode:    each line is a flat selector.
//!                 `F <selector>` declares a first-value field; other columns retain every value.
//!   Grouped mode: a line `G <container>` makes ALL remaining lines sub-fields of one `Many` group
//!                 (the single-pass per-instance × per-sub cost the grouped table measures).
//! Prints one line to stderr: `<bytes> <nsel> <us/page> <pages/s> <MB/s> <vals/page>` (tab-separated).
use std::io::Read;
use std::time::Instant;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let html = std::fs::read(&args[1]).expect("read html file");
    let mut sel_text = String::new();
    std::io::stdin().read_to_string(&mut sel_text).expect("read selectors");

    // Split into flat selectors and (optionally) one grouped `(container, subs)`.
    let mut container: Option<String> = None;
    let mut sels: Vec<String> = Vec::new();
    let mut first_only: Vec<bool> = Vec::new();
    for l in sel_text.lines().map(|l| l.trim()).filter(|l| !l.is_empty()) {
        if let Some(c) = l.strip_prefix("G ") {
            container = Some(c.trim().to_string());
        } else {
            let first = l.strip_prefix("F ");
            first_only.push(first.is_some());
            sels.push(first.unwrap_or(l).to_string());
        }
    }
    let groups: Vec<frostwork::GroupQuery> = match &container {
        Some(c) => vec![frostwork::GroupQuery {
            container: c.clone(),
            subfields: sels.iter().enumerate().map(|(i, s)| (i.to_string(), s.clone())).collect(),
        }],
        None => Vec::new(),
    };
    // count all emitted values: flat columns + every grouped sub-cell
    let count = |flat: &[Vec<String>], grouped: &[frostwork::GroupRows]| -> usize {
        flat.iter().enumerate()
            .map(|(i, c)| {
                if first_only[i] {
                    c.len().min(1)
                } else {
                    c.len()
                }
            }).sum::<usize>()
            + grouped
                .iter()
                .flat_map(|rows| rows.iter().flat_map(|row| row.iter().map(|col| col.len())))
                .sum::<usize>()
    };

    let flat_queries: &[String] = if container.is_some() { &[] } else { &sels };
    let iters = std::env::var("FROSTWORK_BENCH_ITERS")
        .ok()
        .and_then(|s| s.parse::<u32>().ok())
        .unwrap_or(3000);

    // The schema is compiled ONCE, outside the loop, because that is the only way a scraper runs: a
    // Scrapy/web-poet page object is a class, its selectors are declared at class-definition time, and
    // `FrostPage` compiles them into a `Plan` there — once per process, not once per response. Timing
    // `extract_grouped` in the loop re-parsed every selector string per page, which measures a program
    // nobody writes and taxes the high-selector-count cells hardest.
    let plan = frostwork::Plan::compile_first_only(flat_queries, &groups, &first_only);
    let warmup = 200.min(iters.max(1));
    for _ in 0..warmup {
        let _ = plan.extract(&html, None);
    }
    let t = Instant::now();
    let mut nvals = 0usize;
    for _ in 0..iters {
        let (flat, grouped) = plan.extract(&html, None);
        nvals += count(&flat, &grouped);
    }
    let el = t.elapsed().as_secs_f64();
    let per = el / iters as f64;
    // machine-readable to stderr: bytes, nsel, us/page, pages/s, MB/s, vals/page
    eprintln!(
        "{}\t{}\t{:.1}\t{:.0}\t{:.1}\t{}",
        html.len(),
        sels.len(),
        per * 1e6,
        1.0 / per,
        html.len() as f64 / per / 1e6,
        nvals / iters as usize
    );
}
