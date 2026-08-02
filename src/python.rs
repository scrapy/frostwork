//! PyO3 bindings — expose the one-pass `extract` primitive to Python as the native module
//! `frostwork._frostwork`. Deliberately minimal: only the hot path crosses the FFI boundary. The
//! ergonomic `Page`/`Item` layer and the web-poet integration ride on top of this in pure Python
//! (`python/frostwork/`), so there is nothing to keep in sync between two implementations of the
//! matching logic — there is only one (the Rust core).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A schema over the fixed-width bitset budgets is a *caller* bug (too many selectors), distinct from
/// an unsupported *query* (which is contract-defined to yield an empty column). Silence would be the
/// worst outcome — the caller would just see empty columns — so raise `ValueError` instead.
fn check_budget(queries: &[String], groups: &[crate::GroupQuery]) -> PyResult<()> {
    budget_error(crate::budget_usage(queries, groups))
}

/// The `(container, [(name, selector)])` tuples Python passes, as the engine's `GroupQuery` list.
fn group_queries(groups: Vec<(String, Vec<(String, String)>)>) -> Vec<crate::GroupQuery> {
    groups
        .into_iter()
        .map(|(container, subfields)| crate::GroupQuery { container, subfields })
        .collect()
}

/// Raise `ValueError` if a schema's `(members, sibling-bits)` demand exceeds the fixed-width budget.
fn budget_error((members, sib): (usize, usize)) -> PyResult<()> {
    if members > crate::MAX_MEMBERS {
        return Err(PyValueError::new_err(format!(
            "frostwork: schema has {members} member selectors, over the limit of {} \
             (each comma-group member and each group container counts as one). Split the schema.",
            crate::MAX_MEMBERS
        )));
    }
    if sib > crate::MAX_SIB_BITS {
        return Err(PyValueError::new_err(format!(
            "frostwork: schema needs {sib} sibling-combinator trigger bits, over the limit of {}. \
             Reduce the number of `+`/`~` selectors.",
            crate::MAX_SIB_BITS
        )));
    }
    Ok(())
}

/// A schema compiled ONCE and reused across pages — the native object behind `frostwork.Page` /
/// `FrostPage`, which build a `Plan` a single time and call it per response instead of re-sending
/// string selectors (and re-parsing them) every page. The budget is validated at construction, so an
/// over-budget schema raises `ValueError` here — once — not on every extract.
#[pyclass]
struct Plan {
    inner: crate::Plan,
}

#[pymethods]
impl Plan {
    /// Compile `flat_queries` + `groups` (the shapes `extract_grouped` accepts). Raises `ValueError`
    /// if the schema exceeds the member / sibling-bit budget.
    #[new]
    #[pyo3(signature = (flat_queries, groups))]
    fn new(flat_queries: Vec<String>, groups: Vec<(String, Vec<(String, String)>)>) -> PyResult<Self> {
        let inner = crate::Plan::compile(&flat_queries, &group_queries(groups));
        budget_error(inner.budget_usage())?;
        Ok(Plan { inner })
    }

    /// One streaming pass over `html`, returning one value-column per flat query (query order).
    /// The GIL is released for the duration of the scan (`html` is an immutable `bytes` buffer and
    /// the compiled plan is read-only), so concurrent extracts on a thread pool run in parallel.
    #[pyo3(signature = (html, encoding=None))]
    fn extract(&self, py: Python<'_>, html: &[u8], encoding: Option<&str>) -> Vec<Vec<String>> {
        py.detach(|| self.inner.extract(html, encoding).0)
    }

    /// One streaming pass returning `(flat_columns, grouped)` — see the `extract_grouped` free function.
    /// Releases the GIL for the duration of the scan, like `extract`.
    #[pyo3(signature = (html, encoding=None))]
    #[allow(clippy::type_complexity)]
    fn extract_grouped(
        &self,
        py: Python<'_>,
        html: &[u8],
        encoding: Option<&str>,
    ) -> (Vec<Vec<String>>, Vec<Vec<Vec<Vec<String>>>>) {
        py.detach(|| self.inner.extract(html, encoding))
    }
}

/// One streaming pass over `html`, returning one value-column per query (in query order) — the exact
/// output of [`crate::extract`]. `html` must be `bytes` (or a `bytes` subclass such as web-poet's
/// `HttpResponseBody`); the pure-Python `frostwork.extract` wrapper converts other bytes-likes.
/// `encoding` is an optional charset label as Scrapy passes from `Content-Type`; `None` sniffs
/// (BOM → `<meta>` → UTF-8). Unsupported queries yield an empty column — there is no fallback.
/// Raises `ValueError` if the schema exceeds the member/sibling-bit budget (a caller bug).
#[pyfunction]
#[pyo3(signature = (html, queries, encoding=None))]
fn extract(
    py: Python<'_>,
    html: &[u8],
    queries: Vec<String>,
    encoding: Option<&str>,
) -> PyResult<Vec<Vec<String>>> {
    check_budget(&queries, &[])?;
    Ok(py.detach(|| crate::extract(html, &queries, encoding)))
}

/// One streaming pass returning `(flat_columns, grouped)`. `groups` is a list of
/// `(container_selector, [(subfield_name, subfield_selector), ...])`; `grouped[g]` is that group's
/// rows in document order, each row a list of sub-field value-columns (`[group][row][subfield][value]`).
/// Sub-field names are carried by the caller (the pure-Python `Page`/`webpoet` layer); the engine keys
/// sub-columns positionally. Same one-pass, no-DOM, no-fallback semantics as `extract`.
#[pyfunction]
#[pyo3(signature = (html, flat_queries, groups, encoding=None))]
#[allow(clippy::type_complexity)]
fn extract_grouped(
    py: Python<'_>,
    html: &[u8],
    flat_queries: Vec<String>,
    groups: Vec<(String, Vec<(String, String)>)>,
    encoding: Option<&str>,
) -> PyResult<(Vec<Vec<String>>, Vec<Vec<Vec<Vec<String>>>>)> {
    let gq = group_queries(groups);
    check_budget(&flat_queries, &gq)?;
    Ok(py.detach(|| crate::extract_grouped(html, &flat_queries, &gq, encoding)))
}

/// Resolve `label` against the engine's charset-label set (WHATWG labels), returning the canonical
/// encoding name (e.g. `"UTF-8"`, `"windows-1252"`) or `None` if unrecognized. The pure-Python
/// layer uses this to fail fast on labels the engine would otherwise silently ignore (it would
/// fall through to BOM/`<meta>` sniffing — a plausible-wrong-decode, which the no-fallback
/// philosophy forbids surfacing silently).
#[pyfunction]
fn resolve_label(label: &str) -> Option<&'static str> {
    encoding_rs::Encoding::for_label(label.as_bytes()).map(|e| e.name())
}

/// `Support` as a Python-facing `(supported: bool, reason: Optional[str])` tuple.
fn support_tuple(s: &crate::Support) -> (bool, Option<String>) {
    (s.is_supported(), s.reason().map(str::to_string))
}

/// Audit a schema WITHOUT parsing any HTML: for each flat query, group container, and sub-field,
/// report whether the engine supports it (and, if not, a best-effort reason), plus the member /
/// sibling-bit budget usage against the limits. The no-fallback contract makes an unsupported selector
/// indistinguishable from a legitimately-empty field at runtime; this lets a caller check a schema up
/// front. Returns `(flat, groups, budget)` where `flat` is `[(supported, reason)]`, `groups` is
/// `[((c_supported, c_reason), [(supported, reason), ...])]`, and `budget` is
/// `(members, max_members, sib_bits, max_sib_bits)`. The pure-Python `frostwork.check` wraps this into
/// a named report; see `frostwork.page`.
#[pyfunction]
#[allow(clippy::type_complexity)]
fn audit_schema(
    flat_queries: Vec<String>,
    groups: Vec<(String, Vec<(String, String)>)>,
) -> (
    Vec<(bool, Option<String>)>,
    Vec<((bool, Option<String>), Vec<(bool, Option<String>)>)>,
    (usize, usize, usize, usize),
) {
    let a = crate::audit_schema(&flat_queries, &group_queries(groups));
    let flat = a.flat.iter().map(support_tuple).collect();
    let grouped = a
        .groups
        .iter()
        .map(|g| (support_tuple(&g.container), g.subfields.iter().map(support_tuple).collect()))
        .collect();
    (flat, grouped, (a.members, a.max_members, a.sib_bits, a.max_sib_bits))
}

#[pymodule]
fn _frostwork(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__doc__", "Native Frostwork core: treeless, one-pass HTML extraction.")?;
    m.add_function(wrap_pyfunction!(extract, m)?)?;
    m.add_function(wrap_pyfunction!(extract_grouped, m)?)?;
    m.add_function(wrap_pyfunction!(audit_schema, m)?)?;
    m.add_function(wrap_pyfunction!(resolve_label, m)?)?;
    m.add_class::<Plan>()?;
    Ok(())
}
